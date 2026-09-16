import logging
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func as sql_func, text
import io
import csv
import math
from typing import Optional
from datetime import datetime, timezone

from backend.core.database import get_db, SessionLocal
from backend.models.positions import Position
from backend.models.orders import Order
from backend.models.candles import Candle
from backend.models.bots import BotConfig
from backend.models.exchange_keys import ExchangeKey
from backend.core.security import verify_api_key
from backend.core.exchange_registry import build_exchange_from_key
from backend.engine.bot_manager import bot_manager

logger = logging.getLogger("apexalgo.trades")


def _invalidate_drawdown_cache(bot_names):
    """Drop cached drawdown state for the given bots so it is rebuilt from the DB."""
    cache = getattr(bot_manager, "_drawdown_cache", None)
    if not isinstance(cache, dict):
        return
    names = {n for n in bot_names if n}
    for key in [k for k in list(cache) if isinstance(k, tuple) and k and k[0] in names]:
        cache.pop(key, None)

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


router = APIRouter(
    prefix="/api/trades",
    tags=["Trades"],
    dependencies=[Depends(verify_api_key)]
)

@router.get("/positions")
def get_positions(symbol: str = None, mode: str = None, status: str = None, limit: int = Query(default=5000, le=50000), db: Session = Depends(get_db)):
    query = db.query(
        Position.id, Position.exchange, Position.bot_name, Position.symbol,
        Position.mode, Position.status, Position.side, Position.entry_price,
        Position.amount, Position.profit_abs, Position.profit_pct,
        Position.created_at, Position.closed_at
    )
    if symbol:
        formatted_symbol = symbol.replace('-', '/').upper()
        query = query.filter(Position.symbol == formatted_symbol)
    if mode: query = query.filter(Position.mode == mode)
    if status: query = query.filter(Position.status == status)
    query = query.order_by(Position.created_at.desc())
    query = query.limit(limit if limit > 0 else 50000)
    return [
        {"id": r[0], "exchange": r[1], "bot_name": r[2], "symbol": r[3],
         "mode": r[4], "status": r[5], "side": r[6], "entry_price": r[7],
         "amount": r[8], "profit_abs": r[9], "profit_pct": r[10],
         "created_at": r[11].isoformat() if r[11] else None,
         "closed_at": r[12].isoformat() if r[12] else None}
        for r in query.all()
    ]

@router.get("/orders")
def get_orders(symbol: str = None, mode: str = None, limit: int = Query(default=10000, le=50000), db: Session = Depends(get_db)):
    query = db.query(
        Order.id, Order.position_id, Order.exchange, Order.bot_name,
        Order.mode, Order.symbol, Order.side, Order.order_type,
        Order.price, Order.amount, Order.fee, Order.status, Order.timestamp
    )
    if symbol:
        formatted_symbol = symbol.replace('-', '/').upper()
        query = query.filter(Order.symbol == formatted_symbol)
    if mode: query = query.filter(Order.mode == mode)
    query = query.order_by(Order.timestamp.desc())
    query = query.limit(limit if limit > 0 else 50000)
    return [
        {"id": r[0], "position_id": r[1], "exchange": r[2], "bot_name": r[3],
         "mode": r[4], "symbol": r[5], "side": r[6], "order_type": r[7],
         "price": r[8], "amount": r[9], "fee": r[10], "status": r[11],
         "timestamp": r[12].isoformat() if r[12] else None}
        for r in query.all()
    ]

@router.get("/stats")
def get_trade_stats(
    bot_name: str = None, symbol: str = None, exchange: str = None, mode: str = None,
    db: Session = Depends(get_db)
):
    """Server-side trade statistics — avoids sending thousands of records to frontend."""
    query = db.query(Position).filter(Position.status == "closed")
    if bot_name: query = query.filter(Position.bot_name == bot_name)
    if symbol:
        formatted_symbol = symbol.replace('-', '/').upper()
        query = query.filter(Position.symbol == formatted_symbol)
    if exchange: query = query.filter(Position.exchange == exchange)
    if mode: query = query.filter(Position.mode == mode)

    closed = query.all()
    if not closed:
        return {"netPnl": 0, "winRate": 0, "wins": 0, "losses": 0, "total": 0, "profitFactor": 0, "maxDDpct": 0, "avgHoldMs": 0, "sharpe": 0, "totalFees": 0, "avgWin": 0, "avgLoss": 0}

    wins = [p for p in closed if (p.profit_abs or 0) > 0]
    losses = [p for p in closed if (p.profit_abs or 0) <= 0]
    gross_profit = sum(p.profit_abs or 0 for p in wins)
    gross_loss = abs(sum(p.profit_abs or 0 for p in losses))
    net_pnl = gross_profit - gross_loss
    win_rate = (len(wins) / len(closed)) * 100 if closed else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999 if gross_profit > 0 else 0)

    # Max drawdown — percentage of peak equity using backtest_capital
    sorted_pos = sorted(closed, key=lambda p: p.closed_at or datetime.min)
    # Look up backtest_capital from bot configs
    bot_names = list({p.bot_name for p in closed if p.bot_name})
    from backend.models.bots import BotConfig as _BC
    bot_capitals = []
    for bn in bot_names:
        bc = db.query(_BC.settings).filter(_BC.name == bn).first()
        if bc and bc[0]:
            bot_capitals.append(float(bc[0].get("backtest_capital", 1000)))
    starting_capital = max(bot_capitals) if bot_capitals else 1000.0

    equity = starting_capital
    peak_eq = starting_capital
    max_dd_pct = 0.0
    for p in sorted_pos:
        equity += (p.profit_abs or 0)
        if equity > peak_eq: peak_eq = equity
        if peak_eq > 0: max_dd_pct = max(max_dd_pct, ((peak_eq - equity) / peak_eq) * 100)

    # Avg hold time
    hold_times = []
    for p in closed:
        if p.closed_at and p.created_at:
            diff = (p.closed_at - p.created_at).total_seconds() * 1000
            if diff > 0: hold_times.append(diff)
    avg_hold_ms = sum(hold_times) / len(hold_times) if hold_times else 0

    # Simplified Sharpe
    returns = [p.profit_pct or 0 for p in closed]
    mean_ret = sum(returns) / len(returns) if returns else 0
    variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0
    stddev = math.sqrt(variance) if variance > 0 else 0
    sharpe = mean_ret / stddev if stddev > 0 else 0

    # Total fees
    pos_ids = [p.id for p in closed]
    total_fees = 0.0
    if pos_ids:
        fee_result = db.query(sql_func.sum(Order.fee)).filter(Order.position_id.in_(pos_ids)).scalar()
        total_fees = float(fee_result or 0)

    return {
        "netPnl": net_pnl,
        "winRate": win_rate,
        "wins": len(wins),
        "losses": len(losses),
        "total": len(closed),
        "profitFactor": profit_factor,
        "maxDDpct": max_dd_pct,
        "avgHoldMs": avg_hold_ms,
        "sharpe": sharpe,
        "totalFees": total_fees,
        "avgWin": gross_profit / len(wins) if wins else 0,
        "avgLoss": gross_loss / len(losses) if losses else 0,
    }


@router.delete("/bot/{bot_name}")
def delete_bot_trades(bot_name: str, mode: Optional[str] = None, db: Session = Depends(get_db)):
    where = "bot_name = :bot_name"
    params = {"bot_name": bot_name}
    if mode:
        where += " AND mode = :mode"
        params["mode"] = mode

    orders_deleted = _chunked_delete(db, "orders", where, params)
    pos_deleted = _chunked_delete(db, "positions", where, params)
    _invalidate_drawdown_cache([bot_name])
    return {"message": f"Deleted {orders_deleted} orders and {pos_deleted} positions for '{bot_name}' in mode: {mode or 'ALL'}."}

@router.delete("/positions/{position_id}")
def delete_historical_position(position_id: int, db: Session = Depends(get_db)):
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    # Deleting the record of an open real position would leave the coins on
    # the exchange with nothing tracking them — close it first.
    if pos.status != "closed" and pos.mode in ("live", "paper"):
        raise HTTPException(
            status_code=409,
            detail=f"Position #{position_id} is still {pos.status} on the exchange ({pos.mode}). Close it before deleting the record.",
        )

    bot_name = pos.bot_name
    try:
        db.query(Order).filter(Order.position_id == position_id).delete()
        db.delete(pos)
        db.commit()
        _invalidate_drawdown_cache([bot_name])
        return {"status": "success", "message": "Trade permanently deleted."}
    except Exception as e:
        db.rollback()
        logger.error("Failed to delete position %d: %s", position_id, e)
        raise HTTPException(status_code=500, detail="Failed to delete trade.")

@router.post("/positions/bulk-delete")
def bulk_delete_positions(ids: list[int] = Body(...), db: Session = Depends(get_db)):
    """Delete multiple positions and their orders in a single transaction."""
    if not ids:
        return {"deleted": 0}
    open_real = db.query(Position.id).filter(
        Position.id.in_(ids), Position.status != "closed", Position.mode.in_(("live", "paper"))
    ).count()
    if open_real:
        raise HTTPException(status_code=409, detail=f"{open_real} position(s) are still open on the exchange. Close them before deleting the records.")
    try:
        bot_names = [r[0] for r in db.query(Position.bot_name).filter(Position.id.in_(ids)).distinct().all()]
        db.query(Order).filter(Order.position_id.in_(ids)).delete(synchronize_session=False)
        deleted = db.query(Position).filter(Position.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        _invalidate_drawdown_cache(bot_names)
        return {"deleted": deleted}
    except Exception as e:
        db.rollback()
        logger.error("Bulk delete failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to delete trades.")

def _execute_live_close(pos: Position, db: Session):
    """Place a real market order on the exchange to close a live position.

    Returns (fill_price, filled_amount, exit_fee, exchange_order_id).
    Raises HTTPException if the order cannot be placed or is not filled.
    """
    bot = db.query(BotConfig).filter(BotConfig.name == pos.bot_name).first()
    key_name = (bot.settings or {}).get("api_key_name") if bot else None
    if not key_name:
        raise HTTPException(status_code=400, detail="No API key is linked to this bot; cannot close a live position on the exchange.")

    key_record = db.query(ExchangeKey).filter(ExchangeKey.name == key_name).first()
    if not key_record:
        raise HTTPException(status_code=400, detail=f"API key '{key_name}' no longer exists; cannot close a live position on the exchange.")

    ccxt_symbol = pos.symbol.replace('-', '/').upper()
    close_side = "sell" if pos.side == "long" else "buy"

    try:
        exchange = build_exchange_from_key(key_record)
        close_qty = float(exchange.amount_to_precision(ccxt_symbol, pos.amount))
        if close_qty <= 0:
            raise HTTPException(status_code=400, detail="Position amount rounds to zero at exchange precision; cannot place a close order.")
        exch_order = exchange.create_order(ccxt_symbol, "market", close_side, close_qty)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Live close order failed for position %d (%s %s): %s", pos.id, close_side, ccxt_symbol, e, exc_info=True)
        raise HTTPException(status_code=502, detail="Exchange rejected the close order. Position remains open.")

    # Market orders often report status open/None right after creation even though
    # they fill (near-)immediately — poll the exchange for the real fill state.
    exch_order = bot_manager._reconcile_order(exchange, exch_order, ccxt_symbol)
    order_id = exch_order.get("id")
    order_status = exch_order.get("status")
    filled_amount = float(exch_order.get("filled") or 0)

    if filled_amount <= 0:
        if order_status not in ("closed", "canceled", "rejected", "expired") and order_id:
            try:
                exchange.cancel_order(order_id, ccxt_symbol)
                order_status = "canceled"
            except Exception as cancel_exc:
                logger.error("Could not cancel unfilled close order %s for position %d: %s", order_id, pos.id, cancel_exc)
        logger.warning("Live close order %s for position %d not filled (status=%s)", order_id, pos.id, order_status)
        _record_unfilled_close_order(db, pos, close_side, order_id, order_status)
        raise HTTPException(
            status_code=502,
            detail=f"Close order was not filled on the exchange (order {order_id or 'unknown'}, status {order_status or 'unknown'}). Position remains open.",
        )

    if order_status != "closed" and order_id:
        # Partially filled and still resting — cancel the remainder, keep the fill
        try:
            exchange.cancel_order(order_id, ccxt_symbol)
        except Exception as cancel_exc:
            logger.warning("Could not cancel remainder of close order %s for position %d: %s", order_id, pos.id, cancel_exc)

    fill_price = exch_order.get("average") or exch_order.get("price")
    if not fill_price:
        latest_candle = db.query(Candle).filter(Candle.symbol == pos.symbol, Candle.exchange == (pos.exchange or "okx")).order_by(Candle.timestamp.desc()).first()
        fill_price = latest_candle.close if latest_candle else pos.entry_price

    exit_fee = bot_manager._fee_in_quote(exch_order.get("fee"), ccxt_symbol, fill_price)
    return float(fill_price), filled_amount, exit_fee, order_id


def _record_unfilled_close_order(db: Session, pos: Position, close_side: str, order_id, order_status):
    """Persist a DB trace for a close order that did not fill, and reopen the
    position so the whole attempt never leaves an exchange order without a record."""
    try:
        db.rollback()
        db.query(Position).filter(Position.id == pos.id, Position.status == "closing").update(
            {"status": "open"}, synchronize_session=False
        )
        db.add(Order(
            position_id=pos.id,
            exchange=pos.exchange,
            bot_name=pos.bot_name,
            mode=pos.mode,
            symbol=pos.symbol,
            side=close_side,
            order_type="market",
            price=None,
            amount=0.0,
            fee=0.0,
            timestamp=datetime.now(timezone.utc),
            exchange_order_id=order_id,
            status=order_status or "unknown",
        ))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Failed to record unfilled close order %s for position %d: %s", order_id, pos.id, exc)


def close_position_now(pos: Position, db: Session) -> float:
    """Force-close one open position and book the result. Real modes (live, and
    paper = real orders on the exchange sandbox) are closed on the exchange;
    forward_test at the last candle close. Raises HTTPException on failure and
    leaves the position open. Shared by the force-close route and bot deletion."""
    # Atomically mark as closing to prevent a double-close race with the engine
    rows_updated = db.query(Position).filter(
        Position.id == pos.id,
        Position.status == "open"
    ).update({"status": "closing"}, synchronize_session="fetch")
    if rows_updated == 0:
        raise HTTPException(status_code=400, detail="Position is already being closed.")

    try:
        db.refresh(pos)

        exit_fee = 0.0
        exchange_order_id = None
        close_qty = pos.amount

        if pos.mode in ("live", "paper"):
            close_price, close_qty, exit_fee, exchange_order_id = _execute_live_close(pos, db)
        else:
            # Simulated modes: use the most recent candle close price
            latest_candle = db.query(Candle).filter(Candle.symbol == pos.symbol, Candle.exchange == (pos.exchange or "okx")).order_by(Candle.timestamp.desc()).first()
            close_price = latest_candle.close if latest_candle else pos.entry_price

        # Fee-adjusted P&L accumulated on top of earlier partial exits
        filled_buys = [o for o in (pos.orders or []) if o.side == "buy" and o.status == "filled"]
        original_amount = sum((o.amount or 0.0) for o in filled_buys) or pos.amount or close_qty
        total_buy_fees = sum((o.fee or 0.0) for o in filled_buys)
        entry_fee_portion = total_buy_fees * (close_qty / original_amount) if original_amount > 0 else 0.0

        if pos.side == "long":
            realized_pnl = (close_price - pos.entry_price) * close_qty - entry_fee_portion - exit_fee
        else:
            realized_pnl = (pos.entry_price - close_price) * close_qty - entry_fee_portion - exit_fee

        pos.status = "closed"
        pos.closed_at = datetime.now(timezone.utc)
        pos.profit_abs = (pos.profit_abs or 0.0) + realized_pnl
        entry_value = (pos.entry_price or 0.0) * original_amount
        pos.profit_pct = (pos.profit_abs / entry_value) * 100 if entry_value > 0 else 0.0

        close_order = Order(
            position_id=pos.id,
            exchange=pos.exchange,
            bot_name=pos.bot_name,
            mode=pos.mode,
            symbol=pos.symbol,
            side="sell" if pos.side == "long" else "buy",
            order_type="market",
            price=close_price,
            amount=close_qty,
            fee=exit_fee,
            timestamp=datetime.now(timezone.utc),
            exchange_order_id=exchange_order_id,
            status="filled"
        )
        db.add(close_order)
        db.commit()
        _invalidate_drawdown_cache([pos.bot_name])
        return float(close_price)
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("Failed to force close position %d: %s", pos.id, e)
        raise HTTPException(status_code=500, detail="Failed to close position.")


@router.post("/positions/{position_id}/close")
def force_close_position(position_id: int, db: Session = Depends(get_db)):
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        return JSONResponse(status_code=404, content={"detail": "Position not found"})
    if pos.status == "closed":
        return JSONResponse(status_code=400, content={"detail": "Position is already closed."})
    try:
        close_price = close_position_now(pos, db)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    return {"status": "success", "message": f"Position forcefully closed at ${close_price:.2f}"}

@router.get("/export")
def export_trades_csv(mode: str = "live"):
    def generate():
        # The request session is closed once the response starts streaming, so
        # the generator owns its own session for the lifetime of the download
        db = SessionLocal()
        output = io.StringIO()
        writer = csv.writer(output)
        try:
            writer.writerow(["ID", "Timestamp", "Bot Name", "Symbol", "Side", "Type", "Price", "Amount", "Fee", "Exchange Order ID"])
            yield output.getvalue()
            output.seek(0)
            output.truncate()

            for order in db.query(Order).filter(Order.mode == mode, Order.status == "filled").order_by(Order.timestamp.desc()).yield_per(500):
                writer.writerow([
                    order.id, order.timestamp.strftime("%Y-%m-%d %H:%M:%S") if order.timestamp else "",
                    order.bot_name, order.symbol, order.side.upper(), order.order_type.upper(),
                    order.price, order.amount, order.fee, order.exchange_order_id or "N/A"
                ])
                yield output.getvalue()
                output.seek(0)
                output.truncate()
        finally:
            db.close()

    return StreamingResponse(generate(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=apexalgo_{mode}_trades.csv"})
