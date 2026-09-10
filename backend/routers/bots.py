import logging
import json
import re
import asyncio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Body, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from backend.core.database import get_db, SessionLocal
from backend.models.bots import BotConfig
from backend.models.signals import Signal
from backend.models.orders import Order
from backend.models.positions import Position
from backend.core.events import event_bus
from backend.core.security import verify_api_key
from backend.engine.settings_validator import validate_bot_settings
from backend.engine.bot_manager import bot_manager
from backend.core import bot_log_buffer as blb
from backend.models.bot_logs import BotLog
from backend.models.exchange_keys import ExchangeKey


def _resolve_exchange(settings: dict, db: Session) -> str:
    """Resolve the effective exchange for a bot's settings."""
    api_key_name = settings.get("api_key_name")
    if api_key_name:
        key = db.query(ExchangeKey).filter(ExchangeKey.name == api_key_name).first()
        if key:
            return key.exchange
    return settings.get("data_exchange", "okx")

logger = logging.getLogger("apexalgo.bots")

router = APIRouter(
    prefix="/api/bots",
    tags=["Bots"],
    dependencies=[Depends(verify_api_key)]
)

class BotBase(BaseModel):
    name: str
    is_sandbox: bool = True
    strategy: str = "node_evaluator"
    settings: Dict[str, Any] = {}

class BotCreate(BotBase):
    pass

class BotResponse(BotBase):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True

@router.get("/", response_model=List[BotResponse])
def get_all_bots(db: Session = Depends(get_db)):
    return db.query(BotConfig).all()


@router.get("/by-id/{bot_id}", response_model=BotResponse)
def get_bot_by_id(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


@router.get("/summary")
def get_bots_summary(db: Session = Depends(get_db)):
    """Lightweight bot list for polling — excludes full settings/node graph."""
    bots = db.query(BotConfig).all()
    return [
        {
            "id": b.id,
            "name": b.name,
            "is_active": b.is_active,
            "is_sandbox": b.is_sandbox,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "settings": {
                "timeframe": b.settings.get("timeframe") if b.settings else None,
                "symbols": b.settings.get("symbols", []) if b.settings else [],
                "symbol": b.settings.get("symbol") if b.settings else None,
                "api_execution": b.settings.get("api_execution", False) if b.settings else False,
                "api_key_name": b.settings.get("api_key_name") if b.settings else None,
                "backtest_on_start": b.settings.get("backtest_on_start", False) if b.settings else False,
                "backtest_capital": b.settings.get("backtest_capital", 1000) if b.settings else 1000,
                # Needed by chart-open and the Data Vault live-guard: the
                # same pair on another exchange is a different dataset
                "data_exchange": b.settings.get("data_exchange", "okx") if b.settings else "okx",
                "last_backtest_max_drawdown": b.settings.get("last_backtest_max_drawdown") if b.settings else None,
                "last_backtest_summary": b.settings.get("last_backtest_summary") if b.settings else None,
                "last_stop_reason": b.settings.get("last_stop_reason") if b.settings else None,
                "max_drawdown": b.settings.get("max_drawdown") if b.settings else None,
                # Sizing fields for the Analytics capital-allocation panel
                "max_positions": b.settings.get("max_positions", 1) if b.settings else 1,
                "max_order_value": b.settings.get("max_order_value") if b.settings else None,
                "live_allocation_pct": b.settings.get("live_allocation_pct", 100) if b.settings else 100,
                "live_starting_capital": b.settings.get("live_starting_capital") if b.settings else None,
                "entry_amount_type": (b.settings.get("trade_settings") or {}).get("entry", {}).get("amount_type", "percentage") if b.settings else "percentage",
                "entry_amount_value": (b.settings.get("trade_settings") or {}).get("entry", {}).get("amount_value") if b.settings else None,
            },
            # In-memory engine phase (starting / fetching / backtesting / live / halted)
            "runtime": bot_manager.get_runtime(b.name),
        }
        for b in bots
    ]


@router.get("/signals")
def get_bot_signals(symbol: str, timeframe: str, limit: int = Query(default=5000, le=200000), since_id: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    bot_rows = db.query(BotConfig.name, BotConfig.settings).all()

    valid_bot_names = [
        name for name, settings in bot_rows
        if settings and settings.get('timeframe') == timeframe and (symbol in settings.get('symbols', []) or symbol == settings.get('symbol'))
    ]

    if not valid_bot_names:
        return []

    # Use raw column query to avoid ORM overhead on large signal sets
    query = db.query(
        Signal.id, Signal.candle_id, Signal.symbol, Signal.timestamp,
        Signal.bot_name, Signal.name, Signal.action, Signal.value, Signal.extra_data
    ).filter(
        Signal.symbol == symbol,
        Signal.bot_name.in_(valid_bot_names)
    )
    if since_id > 0:
        query = query.filter(Signal.id > since_id)

    rows = query.order_by(Signal.timestamp.desc()).limit(limit).all()
    rows.reverse()

    return [
        {
            "id": sid, "candle_id": cid, "symbol": sym,
            "timestamp": int(ts.replace(tzinfo=timezone.utc).timestamp()) if ts else None,
            "bot_name": bn, "name": nm, "action": act, "value": val,
            "extra_data": ed if isinstance(ed, dict) else (json.loads(ed) if isinstance(ed, str) else {})
        }
        for sid, cid, sym, ts, bn, nm, act, val, ed in rows
    ]

def _sanitize_bot_name(name: str) -> str:
    """Bot names appear in URLs and filenames; strip path-breaking characters."""
    cleaned = "".join("-" if ch in "/\\%#?" else ch for ch in name)
    return " ".join(cleaned.split()).strip()[:100]


@router.post("/")
def create_bot(bot_in: BotCreate, db: Session = Depends(get_db)):
    bot_in.name = _sanitize_bot_name(bot_in.name) or "Unnamed Bot"
    existing_bot = db.query(BotConfig).filter(BotConfig.name == bot_in.name).first()
    if existing_bot:
        raise HTTPException(status_code=400, detail="A bot with this name already exists.")

    resolved_exchange = _resolve_exchange(bot_in.settings, db)
    validation = validate_bot_settings(bot_in.settings, exchange_id=resolved_exchange)
    if validation["errors"]:
        raise HTTPException(status_code=400, detail={"validation_errors": validation["errors"]})

    new_bot = BotConfig(
        name=bot_in.name,
        is_sandbox=bot_in.is_sandbox,
        strategy=bot_in.strategy,
        settings=bot_in.settings,
        is_active=False
    )
    db.add(new_bot)
    db.commit()
    db.refresh(new_bot)

    result = {
        "id": new_bot.id, "name": new_bot.name, "is_sandbox": new_bot.is_sandbox,
        "strategy": new_bot.strategy, "settings": new_bot.settings,
        "is_active": new_bot.is_active, "created_at": new_bot.created_at.isoformat() if new_bot.created_at else None,
    }
    if validation["warnings"]:
        result["validation_warnings"] = validation["warnings"]
    return result

@router.post("/import")
def import_bot(payload: dict = Body(...), db: Session = Depends(get_db)):
    bot_data = payload.get("bot")
    if not isinstance(bot_data, dict):
        raise HTTPException(status_code=400, detail="Invalid file: 'bot' must be an object.")

    name = bot_data.get("name") or "Imported Bot"
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="Invalid file: bot name must be a string.")
    name = _sanitize_bot_name(name) or "Imported Bot"
    if db.query(BotConfig).filter(BotConfig.name == name).first():
        base = f"{name} (imported)"
        name = base
        suffix = 2
        while db.query(BotConfig).filter(BotConfig.name == name).first():
            name = f"{base} {suffix}"
            suffix += 1

    bot_settings = bot_data.get("settings", {})
    if not isinstance(bot_settings, dict):
        raise HTTPException(status_code=400, detail="Invalid file: bot settings must be an object.")
    resolved_exchange = _resolve_exchange(bot_settings, db)
    validation = validate_bot_settings(bot_settings, exchange_id=resolved_exchange)
    if validation["errors"]:
        raise HTTPException(status_code=400, detail={"validation_errors": validation["errors"]})

    new_bot = BotConfig(
        name=name,
        is_sandbox=bot_data.get("is_sandbox", True),
        strategy=bot_data.get("strategy", "node_evaluator"),
        settings=bot_settings,
        is_active=False
    )
    db.add(new_bot)
    db.commit()
    db.refresh(new_bot)

    result = {
        "id": new_bot.id, "name": new_bot.name, "is_sandbox": new_bot.is_sandbox,
        "strategy": new_bot.strategy, "settings": new_bot.settings,
        "is_active": new_bot.is_active, "created_at": new_bot.created_at.isoformat() if new_bot.created_at else None,
    }
    if validation["warnings"]:
        result["validation_warnings"] = validation["warnings"]
    return result

@router.put("/{bot_id}")
def update_bot(bot_id: int, background_tasks: BackgroundTasks, bot_data: dict = Body(...), db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    if bot.is_active:
        raise HTTPException(status_code=409, detail="Cannot update a running bot. Stop it first, then save your changes.")

    if "name" in bot_data and bot_data["name"] != bot.name:
        new_name = _sanitize_bot_name(bot_data["name"]) or bot.name
        existing = db.query(BotConfig).filter(BotConfig.name == new_name, BotConfig.id != bot_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="A bot with this name already exists.")
        old_name = bot.name
        bot.name = new_name
        bot_manager.rename_runtime(old_name, new_name)
        db.query(Signal).filter(Signal.bot_name == old_name).update({"bot_name": new_name}, synchronize_session=False)
        db.query(Order).filter(Order.bot_name == old_name).update({"bot_name": new_name}, synchronize_session=False)
        db.query(Position).filter(Position.bot_name == old_name).update({"bot_name": new_name}, synchronize_session=False)
        db.query(BotLog).filter(BotLog.bot_name == old_name).update({"bot_name": new_name}, synchronize_session=False)

    if "is_sandbox" in bot_data:
        bot.is_sandbox = bot_data["is_sandbox"]

    validation_warnings = []
    if "settings" in bot_data:
        # Re-read settings inside a begin to reduce race window
        current_settings = dict(bot.settings or {})
        for key, value in bot_data["settings"].items():
            current_settings[key] = value

        resolved_exchange = _resolve_exchange(current_settings, db)
        validation = validate_bot_settings(current_settings, exchange_id=resolved_exchange)
        if validation["errors"]:
            raise HTTPException(status_code=400, detail={"validation_errors": validation["errors"]})
        validation_warnings = validation["warnings"]

        bot.settings = current_settings
        flag_modified(bot, "settings")

        # Flush stale signals and backtest data after the response is sent
        background_tasks.add_task(flush_bot_data, bot.name)

    db.commit()
    result = {"message": "Bot configuration updated successfully"}
    if validation_warnings:
        result["validation_warnings"] = validation_warnings
    return result

async def flush_bot_data(bot_name: str):
    def _flush():
        db = SessionLocal()
        try:
            _chunked_delete(db, "signals", "bot_name = :bot_name", {"bot_name": bot_name})
            bt_params = {"bot_name": bot_name, "mode": "backtest"}
            _chunked_delete(db, "orders", "bot_name = :bot_name AND mode = :mode", bt_params)
            _chunked_delete(db, "positions", "bot_name = :bot_name AND mode = :mode", bt_params)
        except Exception as e:
            logger.error("Failed to flush bot data for '%s': %s", bot_name, e)
            db.rollback()
        finally:
            db.close()

    await asyncio.to_thread(_flush)

def _chunked_delete(db: Session, table: str, where: str, params: dict, chunk_size: int = 20000) -> int:
    """Delete rows in small batches with a commit per chunk so the write lock
    is released between chunks and concurrent writers (e.g. backfill) can proceed."""
    total = 0
    while True:
        result = db.execute(
            text(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT {chunk_size})"),
            params,
        )
        db.commit()
        if result.rowcount <= 0:
            break
        total += result.rowcount
    return total

def _cleanup_bot_data(bot_name: str):
    """Background task: delete all trade data and logs for a removed bot."""
    db = SessionLocal()
    params = {"bot_name": bot_name}
    try:
        _chunked_delete(db, "orders", "bot_name = :bot_name", params)
        _chunked_delete(db, "positions", "bot_name = :bot_name", params)
        _chunked_delete(db, "signals", "bot_name = :bot_name", params)
        _chunked_delete(db, "bot_logs", "bot_name = :bot_name", params)
    except Exception as e:
        db.rollback()
        logger.error("Background cleanup failed for '%s': %s", bot_name, e)
    finally:
        db.close()
        bot_manager.unmark_deleted(bot_name)

@router.delete("/{bot_id}")
def delete_bot(bot_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found.")

    if bot.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete a running bot. Stop it first.")

    bot_name = bot.name

    # Real positions must not silently vanish from the books while they still
    # exist on the exchange: close every open non-backtest position first (at
    # market, even at a loss). Any failure aborts the delete with the bot intact.
    from backend.routers.trades import close_position_now
    open_real = db.query(Position).filter(
        Position.bot_name == bot_name, Position.status == "open",
        Position.mode.in_(["forward_test", "paper", "live"]),
    ).all()
    closed_now = 0
    closed_real = 0
    for pos in open_real:
        try:
            price = close_position_now(pos, db)
            closed_now += 1
            closed_real += pos.mode in ("paper", "live")
            logger.warning("Bot '%s' delete: closed %s %s position #%d at %s", bot_name, pos.mode, pos.symbol, pos.id, price)
        except HTTPException as e:
            raise HTTPException(
                status_code=e.status_code if e.status_code >= 500 else 409,
                detail=f"Could not close open {pos.mode} position on {pos.symbol} (#{pos.id}): {e.detail} "
                       f"Bot not deleted — {closed_now} of {len(open_real)} positions were closed.",
            )

    bot_manager.mark_deleted(bot_name)
    db.delete(bot)
    db.commit()

    # Heavy cleanup (orders, positions, signals, logs) runs after response is sent
    background_tasks.add_task(_cleanup_bot_data, bot_name)
    msg = f"Bot '{bot_name}' deleted."
    if closed_real:
        msg += f" {closed_real} open position(s) closed on the exchange first."
    elif closed_now:
        msg += f" {closed_now} open forward-test position(s) closed first."
    return {"message": msg, "is_active": False}

async def _start(bot: BotConfig, db: Session):
    bot.is_active = True
    if bot.settings and bot.settings.get("last_stop_reason"):
        bot.settings = {**bot.settings, "last_stop_reason": None}
        flag_modified(bot, "settings")
    db.commit()
    # Show "queued" immediately — the engine thread replaces this within ms
    bot_manager.set_runtime(bot.name, "starting", "Queued for startup…")
    await event_bus.publish("BOT_STATE_CHANGED", {"bot_id": bot.id, "action": "started"})


async def _stop(bot: BotConfig, db: Session):
    bot.is_active = False
    db.commit()
    bot_manager.clear_runtime(bot.name)
    await event_bus.publish("BOT_STATE_CHANGED", {"bot_id": bot.id, "action": "stopped"})


@router.post("/bulk/start")
async def start_all_bots(ids: Optional[List[int]] = Body(default=None), db: Session = Depends(get_db)):
    """Start every stopped bot (or the given ids). The engine backfills them
    concurrently; each bot reports its own phase in /summary."""
    q = db.query(BotConfig).filter(BotConfig.is_active == False)
    if ids:
        q = q.filter(BotConfig.id.in_(ids))
    started = []
    for bot in q.all():
        await _start(bot, db)
        started.append(bot.name)
    return {"started": started}


@router.post("/bulk/stop")
async def stop_all_bots(ids: Optional[List[int]] = Body(default=None), db: Session = Depends(get_db)):
    q = db.query(BotConfig).filter(BotConfig.is_active == True)
    if ids:
        q = q.filter(BotConfig.id.in_(ids))
    stopped = []
    for bot in q.all():
        await _stop(bot, db)
        stopped.append(bot.name)
    return {"stopped": stopped}


@router.post("/{bot_id}/start")
async def start_bot(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot: raise HTTPException(status_code=404, detail="Bot not found.")
    if bot.is_active: raise HTTPException(status_code=400, detail="Already active.")
    await _start(bot, db)
    return {"message": f"Bot '{bot.name}' started.", "is_active": True}

@router.post("/{bot_id}/stop")
async def stop_bot(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot: raise HTTPException(status_code=404, detail="Bot not found.")
    await _stop(bot, db)
    return {"message": f"Bot '{bot.name}' stopped.", "is_active": False}

@router.post("/{bot_id}/restart")
async def restart_bot(bot_id: int, db: Session = Depends(get_db)):
    """Stop + start in one call. A fresh run token makes the previous startup
    thread (if still backfilling) abort instead of running twice."""
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot: raise HTTPException(status_code=404, detail="Bot not found.")
    if bot.is_active:
        await _stop(bot, db)
    await _start(bot, db)
    return {"message": f"Bot '{bot.name}' restarted.", "is_active": True}

@router.post("/{bot_id}/duplicate")
def duplicate_bot(bot_id: int, db: Session = Depends(get_db)):
    source = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Bot not found.")

    base_name = source.name
    new_name = f"{base_name} (copy)"
    for i in range(2, 11):
        if not db.query(BotConfig).filter(BotConfig.name == new_name).first():
            break
        new_name = f"{base_name} (copy {i})"
    else:
        raise HTTPException(status_code=400, detail="Too many copies of this bot already exist.")

    new_bot = BotConfig(
        name=new_name,
        is_sandbox=source.is_sandbox,
        strategy=source.strategy,
        settings=dict(source.settings or {}),
        is_active=False
    )
    db.add(new_bot)
    db.commit()
    db.refresh(new_bot)
    return {
        "id": new_bot.id, "name": new_bot.name, "is_sandbox": new_bot.is_sandbox,
        "strategy": new_bot.strategy, "settings": new_bot.settings,
        "is_active": new_bot.is_active, "created_at": new_bot.created_at.isoformat() if new_bot.created_at else None,
    }

@router.get("/{bot_id}/export")
def export_bot(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found.")
    payload = {
        "apex_version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "bot": {
            "name": bot.name,
            "is_sandbox": bot.is_sandbox,
            "strategy": bot.strategy,
            "settings": bot.settings or {}
        }
    }
    safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', bot.name).strip('_') or f"bot_{bot.id}"
    filename = f"{safe_name}.apex.json"
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.get("/console/logs")
def get_bot_logs_q(bot_name: str = Query(...), since: int = Query(default=0)):
    """Query-param variant: bot names may contain '/', which breaks path routing."""
    entries = blb.get_logs(bot_name, since_id=since)
    return {"bot_name": bot_name, "entries": entries}


@router.delete("/console/cache")
def clear_bot_cache_q(bot_name: str = Query(...), db: Session = Depends(get_db)):
    return clear_bot_cache(bot_name, db)


@router.get("/{bot_name}/logs")
def get_bot_logs(bot_name: str, since: int = Query(default=0)):
    entries = blb.get_logs(bot_name, since_id=since)
    return {"bot_name": bot_name, "entries": entries}

@router.delete("/{bot_name}/cache")
def clear_bot_cache(bot_name: str, db: Session = Depends(get_db)):
    """Reset everything the engine derived for a bot so the next start is a
    clean run: signals, console logs and in-memory caches always; simulated
    trades (backtest + forward_test) too when the bot is stopped. Real (paper/
    live) trades are never touched here."""
    try:
        bot = db.query(BotConfig).filter(BotConfig.name == bot_name).first()
        running = bool(bot and bot.is_active)
        result = db.execute(text("DELETE FROM signals WHERE bot_name = :bn"), {"bn": bot_name})
        deleted_signals = result.rowcount
        deleted_sim = 0
        if not running:
            for mode in ("backtest", "forward_test"):
                params = {"bn": bot_name, "mode": mode}
                db.execute(text("DELETE FROM orders WHERE bot_name = :bn AND mode = :mode"), params)
                deleted_sim += db.execute(text("DELETE FROM positions WHERE bot_name = :bn AND mode = :mode"), params).rowcount
            if bot and bot.settings and any(k in bot.settings for k in ("last_backtest_summary", "last_backtest_max_drawdown", "last_stop_reason", "drawdown_peak_reset_at", "live_starting_capital")):
                bot.settings = {k: v for k, v in bot.settings.items() if k not in ("last_backtest_summary", "last_backtest_max_drawdown", "last_stop_reason", "drawdown_peak_reset_at", "live_starting_capital")}
                flag_modified(bot, "settings")
        db.commit()
        blb.clear(bot_name)
        bot_manager.reset_bot_state(bot_name)
        msg = f"Cache cleared: {deleted_signals} signals removed, log buffer and engine caches reset."
        if running:
            msg += " Bot is running — simulated trades kept; stop it first for a full reset."
        else:
            msg += f" {deleted_sim} simulated (backtest/forward-test) positions removed."
        return {"status": "success", "message": msg}
    except Exception as e:
        db.rollback()
        logger.error("Failed to clear cache for '%s': %s", bot_name, e)
        raise HTTPException(status_code=500, detail="Failed to clear cache.")
