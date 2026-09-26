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
from backend.engine.bot_manager import bot_manager, _config_fingerprint
from backend.engine.sizing import backtest_pin
from backend.engine.symbols import cash_currency as _cash_currency_of
from backend.engine.data_verify import verify_window
from backend.core.exchange_registry import build_exchange_for_symbol
from backend.core import bot_log_buffer as blb
from backend.models.bot_logs import BotLog
from backend.models.bot_config_runs import BotConfigRun
from backend.models.exchange_keys import ExchangeKey


def _resolve_exchange(settings: dict, db: Session) -> str:
    """Resolve the effective exchange for a bot's settings."""
    api_key_name = settings.get("api_key_name")
    if api_key_name:
        key = db.query(ExchangeKey).filter(ExchangeKey.name == api_key_name).first()
        if key:
            return key.exchange
    return settings.get("data_exchange", "okx")


def _resolve_key_market_type(settings: dict, db: Session) -> str | None:
    """Market type of the linked API key (None when no key is linked); a key
    is bound to one market, so the validator refuses a bot on another."""
    api_key_name = settings.get("api_key_name")
    if api_key_name:
        key = db.query(ExchangeKey).filter(ExchangeKey.name == api_key_name).first()
        if key:
            return getattr(key, "market_type", None) or "spot"
    return None


def _candle_exchange(settings: dict, key_exchange_by_name: dict) -> str:
    """The exchange this bot's candles (and therefore its signals) come from —
    the same rule as engine startup: an API key that actually routes orders
    wins, otherwise the configured data exchange."""
    settings = settings or {}
    if settings.get("api_execution") and settings.get("api_key_name"):
        key_exchange = key_exchange_by_name.get(settings["api_key_name"])
        if key_exchange:
            return key_exchange
    return settings.get("data_exchange") or "okx"


def _key_exchanges(db: Session) -> dict:
    return {name: ex for name, ex in db.query(ExchangeKey.name, ExchangeKey.exchange).all()}

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
    # Effective candle/signal exchange (key exchange when orders are routed,
    # else data_exchange) — the chart uses it to scope overlays to a dataset
    exchange: Optional[str] = None

    class Config:
        from_attributes = True

@router.get("/", response_model=List[BotResponse])
def get_all_bots(db: Session = Depends(get_db)):
    key_exchanges = _key_exchanges(db)
    out = []
    for b in db.query(BotConfig).all():
        item = BotResponse.model_validate(b)
        item.exchange = _candle_exchange(b.settings, key_exchanges)
        out.append(item)
    return out


@router.get("/by-id/{bot_id}", response_model=BotResponse)
def get_bot_by_id(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


def _execution_mode(settings: dict, sandbox_by_key: dict) -> str:
    """The mode this bot's fills are booked in — the same rule the engine
    applies: an API key routes orders (paper on a sandbox key, live otherwise),
    no key means a local forward test. The UI shows this word, not a guess
    from api_execution alone."""
    settings = settings or {}
    key_name = settings.get("api_key_name")
    if settings.get("api_execution") and key_name and key_name in sandbox_by_key:
        return "paper" if sandbox_by_key[key_name] else "live"
    return "forward_test"


def _bot_cash_currency(settings) -> str | None:
    """Cash currency of a bot from its whitelist (the persisted backtest
    summary's `cash_currency` wins when present: it was computed with the
    exchange's market data)."""
    if not settings:
        return None
    summary = settings.get("last_backtest_summary") or {}
    if summary.get("cash_currency"):
        return summary["cash_currency"]
    for sym in list(settings.get("symbols") or []) + ([settings["symbol"]] if settings.get("symbol") else []):
        norm = str(sym or "").replace("-", "/").upper()
        if "/" in norm:
            return _cash_currency_of(norm)
    return None


@router.get("/summary")
def get_bots_summary(db: Session = Depends(get_db)):
    """Lightweight bot list for polling — excludes full settings/node graph."""
    bots = db.query(BotConfig).all()
    sandbox_by_key = {k.name: bool(k.is_sandbox) for k in db.query(ExchangeKey.name, ExchangeKey.is_sandbox).all()}
    key_exchanges = _key_exchanges(db)
    return [
        {
            "id": b.id,
            "name": b.name,
            "is_active": b.is_active,
            "is_sandbox": b.is_sandbox,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "execution_mode": _execution_mode(b.settings, sandbox_by_key),
            # Where this bot's candles come from (key exchange when routing
            # orders, else data_exchange) — chart-open and Data Vault key on it
            "exchange": _candle_exchange(b.settings, key_exchanges),
            "settings": {
                "timeframe": b.settings.get("timeframe") if b.settings else None,
                "symbols": b.settings.get("symbols", []) if b.settings else [],
                "symbol": b.settings.get("symbol") if b.settings else None,
                "api_execution": b.settings.get("api_execution", False) if b.settings else False,
                "api_key_name": b.settings.get("api_key_name") if b.settings else None,
                "backtest_on_start": b.settings.get("backtest_on_start", False) if b.settings else False,
                "backtest_capital": b.settings.get("backtest_capital", 1000) if b.settings else 1000,
                # Unit `backtest_capital`, PnL and the pool are in: the quote
                # of the whitelist (settle on swaps) — never a hard-coded USD
                "cash_currency": _bot_cash_currency(b.settings),
                "backtest_from": b.settings.get("backtest_from") if b.settings else None,
                "backtest_to": b.settings.get("backtest_to") if b.settings else None,
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
                # Phase 2: swap bots carry leverage (chips on card/Analytics/Home)
                "market_type": (b.settings.get("market_type") or "spot") if b.settings else "spot",
                "leverage": b.settings.get("leverage", 1) if b.settings else 1,
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
def get_bot_signals(symbol: str, timeframe: str, exchange: str = Query(default="okx"), limit: int = Query(default=5000, le=200000), since_id: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    bot_rows = db.query(BotConfig.name, BotConfig.settings).all()
    key_exchanges = _key_exchanges(db)
    exchange = exchange.lower()

    # Same pair + interval on another exchange is a different dataset: only
    # bots that trade this exact (exchange, symbol, timeframe) belong on the chart
    valid_bot_names = [
        name for name, settings in bot_rows
        if settings and settings.get('timeframe') == timeframe
        and (symbol in settings.get('symbols', []) or symbol == settings.get('symbol'))
        and _candle_exchange(settings, key_exchanges).lower() == exchange
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
    validation = validate_bot_settings(bot_in.settings, exchange_id=resolved_exchange, key_market_type=_resolve_key_market_type(bot_in.settings, db))
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
    validation = validate_bot_settings(bot_settings, exchange_id=resolved_exchange, key_market_type=_resolve_key_market_type(bot_settings, db))
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
        db.query(BotConfigRun).filter(BotConfigRun.bot_name == old_name).update({"bot_name": new_name}, synchronize_session=False)

    if "is_sandbox" in bot_data:
        bot.is_sandbox = bot_data["is_sandbox"]

    validation_warnings = []
    if "settings" in bot_data:
        # Re-read settings inside a begin to reduce race window
        current_settings = dict(bot.settings or {})
        for key, value in bot_data["settings"].items():
            current_settings[key] = value

        resolved_exchange = _resolve_exchange(current_settings, db)
        validation = validate_bot_settings(current_settings, exchange_id=resolved_exchange, key_market_type=_resolve_key_market_type(current_settings, db))
        if validation["errors"]:
            raise HTTPException(status_code=400, detail={"validation_errors": validation["errors"]})
        validation_warnings = validation["warnings"]

        old_fp = _config_fingerprint(bot.settings)
        bot.settings = current_settings
        flag_modified(bot, "settings")

        # Signals and backtest trades belong to a strategy configuration: flush
        # them only when that changed, not on a layout/routing-only save
        if _config_fingerprint(current_settings) != old_fp:
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
        _chunked_delete(db, "bot_config_runs", "bot_name = :bot_name", params)
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


async def _deactivate(bot: BotConfig, db: Session):
    bot.is_active = False
    db.commit()
    bot_manager.clear_runtime(bot.name)
    await event_bus.publish("BOT_STATE_CHANGED", {"bot_id": bot.id, "action": "stopped"})


def _open_real_positions(bot_name: str, db: Session) -> List[Position]:
    """Open positions that exist outside the backtest ledger: forward test
    (simulated but tracked by the running engine) and paper/live (real orders)."""
    return db.query(Position).filter(
        Position.bot_name == bot_name, Position.status == "open",
        Position.mode.in_(["forward_test", "paper", "live"]),
    ).order_by(Position.id).all()


def _position_brief(pos: Position) -> dict:
    return {"id": pos.id, "symbol": pos.symbol, "mode": pos.mode, "amount": pos.amount,
            "entry_price": pos.entry_price, "exchange": pos.exchange}


async def _stop(bot: BotConfig, db: Session, close_positions: Optional[bool] = None) -> dict:
    """Deactivate a bot. A user-initiated stop with open non-backtest positions
    is an explicit choice: ``close_positions=None`` refuses with 409 and the
    list of positions, ``True`` closes them at market first (like delete),
    ``False`` leaves them open and unmanaged (no SL/TP) with a console warning.
    Engine-initiated stops (risk breach, unknown order) never come through here —
    they always close everything themselves."""
    open_real = _open_real_positions(bot.name, db)
    if open_real and close_positions is None:
        raise HTTPException(status_code=409, detail={
            "message": f"Bot '{bot.name}' has {len(open_real)} open position(s). Choose whether to close them.",
            "open_positions": [_position_brief(p) for p in open_real],
        })

    # Deactivate first so the engine skips this bot on the next tick; the
    # open→closing update inside close_position_now guards against a tick that
    # is already mid-flight.
    await _deactivate(bot, db)

    closed = []
    if open_real and close_positions:
        from backend.routers.trades import close_position_now
        for pos in open_real:
            try:
                price = close_position_now(pos, db)
                closed.append({**_position_brief(pos), "close_price": price})
                blb.push(bot.name, "INFO", f"Stopped by user: closed {pos.mode} {pos.symbol} position #{pos.id} at {price}")
            except HTTPException as e:
                remaining = [p for p in open_real if p.id not in {c['id'] for c in closed}]
                blb.push(bot.name, "ERROR", f"Stopped by user but could not close {pos.mode} {pos.symbol} position #{pos.id}: {e.detail} "
                                            f"— {len(remaining)} position(s) left open and unmanaged (no SL/TP).")
                raise HTTPException(
                    status_code=e.status_code if e.status_code >= 500 else 409,
                    detail={
                        "message": f"Bot '{bot.name}' is stopped, but closing {pos.mode} {pos.symbol} position #{pos.id} failed: {e.detail} "
                                   f"{len(closed)} of {len(open_real)} positions were closed; the rest are open and unmanaged.",
                        "open_positions": [_position_brief(p) for p in remaining],
                        "closed_positions": closed,
                    },
                )
    elif open_real:
        blb.push(bot.name, "WARN", f"Stopped by user with {len(open_real)} open position(s) left unmanaged — no stop-loss or take-profit "
                                   f"will fire: {', '.join(f'{p.mode} {p.symbol} #{p.id}' for p in open_real)}")

    return {"closed_positions": closed, "unmanaged_positions": [] if close_positions else [_position_brief(p) for p in open_real]}


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
async def stop_all_bots(ids: Optional[List[int]] = Body(default=None),
                        close_positions: Optional[bool] = Query(default=None),
                        db: Session = Depends(get_db)):
    """Same contract as the single stop: without ``close_positions`` the call
    refuses (409) when any selected bot has open non-backtest positions, and
    lists them per bot so the UI can ask once for the whole batch."""
    q = db.query(BotConfig).filter(BotConfig.is_active == True)
    if ids:
        q = q.filter(BotConfig.id.in_(ids))
    bots = q.all()
    if close_positions is None:
        blocking = {b.name: [_position_brief(p) for p in _open_real_positions(b.name, db)] for b in bots}
        blocking = {k: v for k, v in blocking.items() if v}
        if blocking:
            raise HTTPException(status_code=409, detail={
                "message": f"{len(blocking)} bot(s) have open positions. Choose whether to close them.",
                "open_positions": [{**p, "bot_name": name} for name, ps in blocking.items() for p in ps],
                "bots": blocking,
            })
    stopped, closed, unmanaged, failed = [], [], [], []
    for bot in bots:
        try:
            res = await _stop(bot, db, close_positions=close_positions)
        except HTTPException as e:
            failed.append({"bot_name": bot.name, "detail": e.detail})
            stopped.append(bot.name)  # deactivation happened before the close attempt
            continue
        stopped.append(bot.name)
        closed += [{**p, "bot_name": bot.name} for p in res["closed_positions"]]
        unmanaged += [{**p, "bot_name": bot.name} for p in res["unmanaged_positions"]]
    if failed:
        raise HTTPException(status_code=409, detail={
            "message": f"All {len(stopped)} bot(s) stopped, but closing positions failed for {len(failed)} bot(s).",
            "failed": failed, "closed_positions": closed,
        })
    return {"stopped": stopped, "closed_positions": closed, "unmanaged_positions": unmanaged}


@router.post("/{bot_id}/start")
async def start_bot(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot: raise HTTPException(status_code=404, detail="Bot not found.")
    if bot.is_active: raise HTTPException(status_code=400, detail="Already active.")
    await _start(bot, db)
    return {"message": f"Bot '{bot.name}' started.", "is_active": True}

@router.post("/{bot_id}/stop")
async def stop_bot(bot_id: int, close_positions: Optional[bool] = Query(default=None), db: Session = Depends(get_db)):
    """Stop a bot. With open forward-test/paper/live positions this is refused
    (409, ``detail.open_positions``) until the caller passes
    ``?close_positions=true`` (close at market, then stop) or ``false`` (stop and
    leave them open — no SL/TP will fire)."""
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot: raise HTTPException(status_code=404, detail="Bot not found.")
    res = await _stop(bot, db, close_positions=close_positions)
    msg = f"Bot '{bot.name}' stopped."
    if res["closed_positions"]:
        msg += f" {len(res['closed_positions'])} open position(s) closed first."
    elif res["unmanaged_positions"]:
        msg += f" {len(res['unmanaged_positions'])} position(s) left open and unmanaged."
    return {"message": msg, "is_active": False, **res}

@router.post("/{bot_id}/restart")
async def restart_bot(bot_id: int, db: Session = Depends(get_db)):
    """Stop + start in one call. A fresh run token makes the previous startup
    thread (if still backfilling) abort instead of running twice."""
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot: raise HTTPException(status_code=404, detail="Bot not found.")
    if bot.is_active:
        # Positions stay managed: the restarted engine picks them up again
        await _deactivate(bot, db)
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

@router.post("/{bot_id}/verify-data")
async def verify_bot_data(bot_id: int, accept: bool = Query(default=False), db: Session = Depends(get_db)):
    """Check the candles of the bot's last backtest window (pinned window or
    the range the last run walked) against the exchange, per whitelist
    symbol. Stored candles never change on their own, so this is the only way
    to learn about an exchange restatement. ``accept=true`` overwrites the
    restated rows — after which the next run on that slice will flag
    'data changed'. The outcome is kept in ``last_backtest_summary``."""
    bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found.")
    settings = bot.settings or {}
    summary = settings.get("last_backtest_summary") or {}
    pin_from, pin_to = backtest_pin(settings)
    start = pin_from or summary.get("window_from") or summary.get("data_from")
    end = pin_to or summary.get("window_to") or summary.get("data_to")
    if not start or not end:
        raise HTTPException(status_code=400, detail="No backtest window to verify yet — run a backtest first.")
    if isinstance(start, str):
        start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
    symbols = settings.get("symbols") or ([settings["symbol"]] if settings.get("symbol") else [])
    timeframe = settings.get("timeframe")
    exchange_id = _candle_exchange(settings, _key_exchanges(db))

    def _run():
        _db = SessionLocal()
        try:
            return [verify_window(_db, build_exchange_for_symbol(exchange_id, sym), exchange_id, sym, timeframe, start, end, accept=accept)
                    for sym in (str(s).replace('-', '/').upper() for s in symbols)]
        finally:
            _db.close()
    try:
        results = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error("Verify failed for bot '%s': %s", bot.name, e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not verify against {exchange_id}: {str(e)[:160]}")

    restated = sum(r["restated_count"] for r in results)
    accepted = sum(r["accepted"] for r in results)
    missing = sum(r["missing_local"] for r in results)
    verified_at = datetime.now(timezone.utc).isoformat()
    if summary:
        bot.settings = {**settings, "last_backtest_summary": {
            **summary, "verified_at": verified_at, "restated_candles": restated - accepted, "missing_local": missing,
        }}
        flag_modified(bot, "settings")
        db.commit()
    for r in results:
        if r["restated_count"]:
            blb.push(bot.name, "WARN" if not accept else "INFO",
                     f"{exchange_id} restated {r['restated_count']} candle(s) of {r['symbol']} {timeframe} in "
                     f"{r['from'][:10]} → {r['to'][:10]}" + (f" — {r['accepted']} overwritten locally" if accept else " — local snapshot kept"))
        else:
            blb.push(bot.name, "INFO", f"Verified {r['symbol']} {timeframe}: {r['checked']} candles match {exchange_id}")
    return {"exchange": exchange_id, "from": results[0]["from"] if results else None, "to": results[0]["to"] if results else None,
            "restated": restated, "accepted": accepted, "missing_local": missing, "verified_at": verified_at, "symbols": results}


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
            # The pinned backtest window belongs to this machine's candle
            # store; on another install it would replay a range that may
            # not exist there
            "settings": {k: v for k, v in (bot.settings or {}).items() if k not in ("backtest_from", "backtest_to")},
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
        # Variant counter starts over: a reset is a clean slate for the strategy too
        db.execute(text("DELETE FROM bot_config_runs WHERE bot_name = :bn"), {"bn": bot_name})
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
