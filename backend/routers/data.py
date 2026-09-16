import logging
import time
from datetime import datetime, timezone
from typing import Optional
import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from backend.core.database import get_db, SessionLocal
from backend.models.candles import Candle
from backend.core.security import verify_api_key
from backend.core.exchange_registry import build_exchange, SUPPORTED_EXCHANGES, get_exchange_timeframes

logger = logging.getLogger("apexalgo.data")

# TTL cache for market-info responses (reduces exchange API calls)
_ticker_cache: dict[str, tuple[float, dict]] = {}
_TICKER_TTL = 10  # seconds

router = APIRouter(
    prefix="/api/data",
    tags=["Market Data"],
    dependencies=[Depends(verify_api_key)]
)


@router.get("/timeframes/{exchange_id}")
async def get_timeframes(exchange_id: str):
    """Return supported timeframes for an exchange."""
    exchange_id = exchange_id.lower()
    if exchange_id not in SUPPORTED_EXCHANGES:
        raise HTTPException(status_code=400, detail=f"Unknown exchange '{exchange_id}'.")
    tf_map = await asyncio.to_thread(get_exchange_timeframes, exchange_id)
    return {"exchange": exchange_id, "timeframes": list(tf_map.keys())}


class HistoricalDataFetch(BaseModel):
    exchange: str = "okx"
    timeframe: str
    start_date: datetime
    end_date: datetime


@router.post("/fetch/{symbol}")
async def fetch_historical_data(
    symbol: str,
    req: HistoricalDataFetch
):
    exchange_id = req.exchange.lower()
    if exchange_id not in SUPPORTED_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported exchange '{exchange_id}'. Supported: {', '.join(SUPPORTED_EXCHANGES)}"
        )
    if not req.timeframe:
        raise HTTPException(status_code=400, detail="timeframe is required.")

    # Validate timeframe is supported by the exchange
    tf_map = await asyncio.to_thread(get_exchange_timeframes, exchange_id)
    if tf_map and req.timeframe not in tf_map:
        supported = ', '.join(sorted(tf_map.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Exchange '{exchange_id}' does not support timeframe '{req.timeframe}'. Supported: {supported}"
        )
    try:
        formatted_symbol = symbol.replace('-', '/').upper()
        result = await asyncio.to_thread(_fetch_and_save_data, formatted_symbol, exchange_id, req)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in manual sync: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch historical data from the exchange.")


def _fetch_and_save_data(formatted_symbol: str, exchange_id: str, req: HistoricalDataFetch):
    exch = build_exchange(exchange_id)

    start_ts = int(req.start_date.timestamp() * 1000)
    end_ts = int(req.end_date.timestamp() * 1000)

    current_since = start_ts
    total_fetched = 0
    total_saved = 0
    actual_oldest_ts = None

    logger.info(
        "Manual sync: %s/%s/%s — fetching from %s to %s.",
        exchange_id, formatted_symbol, req.timeframe, req.start_date, req.end_date
    )

    db = SessionLocal()
    try:
        while current_since < end_ts:
            try:
                batch = exch.fetch_ohlcv(
                    formatted_symbol, req.timeframe, since=current_since, limit=500
                )
            except Exception as e:
                logger.warning("Fetch error %s/%s/%s: %s", exchange_id, formatted_symbol, req.timeframe, e)
                break

            if not batch:
                if total_fetched == 0 and current_since == start_ts:
                    # No data at requested start — jump forward to find where data begins
                    found = False
                    probe_since = start_ts
                    jump = (end_ts - start_ts) // 4
                    while probe_since < end_ts:
                        probe_since += jump
                        try:
                            probe = exch.fetch_ohlcv(formatted_symbol, req.timeframe, since=probe_since, limit=10)
                        except Exception:
                            probe = None
                        time.sleep(0.35)
                        if probe:
                            current_since = int(probe[0][0])
                            found = True
                            break
                    if found:
                        continue
                break

            # Filter to requested date range
            valid = [c for c in batch if start_ts <= int(c[0]) <= end_ts]

            if valid:
                total_fetched += len(valid)

                batch_start_dt = datetime.fromtimestamp(int(valid[0][0]) / 1000.0, tz=timezone.utc)
                batch_end_dt = datetime.fromtimestamp(int(valid[-1][0]) / 1000.0, tz=timezone.utc)

                existing = db.query(Candle.timestamp).filter(
                    Candle.exchange == exchange_id,
                    Candle.symbol == formatted_symbol,
                    Candle.timeframe == req.timeframe,
                    Candle.timestamp >= batch_start_dt,
                    Candle.timestamp <= batch_end_dt,
                ).all()
                existing_times = {
                    r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0]
                    for r in existing
                }

                new_candles = []
                for row in valid:
                    ts = datetime.fromtimestamp(int(row[0]) / 1000.0, tz=timezone.utc)
                    if ts not in existing_times:
                        new_candles.append(Candle(
                            exchange=exchange_id,
                            symbol=formatted_symbol,
                            timeframe=req.timeframe,
                            timestamp=ts,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            marketcap=0.0,
                        ))

                if new_candles:
                    db.bulk_save_objects(new_candles)
                    db.commit()
                    total_saved += len(new_candles)

                if actual_oldest_ts is None:
                    actual_oldest_ts = int(valid[0][0])

            last_ts = int(batch[-1][0])

            # Stop if exchange returned data beyond our range or returned a short batch
            if last_ts >= end_ts or len(batch) < 2:
                break

            current_since = last_ts + 1
            time.sleep(0.35)

        if total_fetched == 0:
            return {
                "message": f"{SUPPORTED_EXCHANGES[exchange_id]} has no data available for this symbol/timeframe in the requested range.",
                "total_fetched": 0,
                "new_saved": 0,
            }

        msg = "Download complete."
        if actual_oldest_ts and actual_oldest_ts > start_ts:
            actual_start_dt = datetime.fromtimestamp(actual_oldest_ts / 1000.0, tz=timezone.utc)
            msg = f"Download complete. {SUPPORTED_EXCHANGES[exchange_id]} history only goes back to {actual_start_dt.strftime('%Y-%m-%d')} for this pair/interval."

        return {
            "message": msg,
            "total_fetched": total_fetched,
            "new_saved": total_saved,
            "actual_start": datetime.fromtimestamp(actual_oldest_ts / 1000.0, tz=timezone.utc).isoformat() if actual_oldest_ts else None,
        }
    finally:
        db.close()


@router.get("/exchanges")
def get_supported_exchanges():
    """Return the list of supported exchanges for UI dropdowns."""
    return [{"id": k, "name": v} for k, v in SUPPORTED_EXCHANGES.items()]


@router.get("/summary")
def get_data_summary(db: Session = Depends(get_db)):
    summary = db.query(
        Candle.exchange,
        Candle.symbol,
        Candle.timeframe,
        func.min(Candle.timestamp).label("oldest_candle"),
        func.max(Candle.timestamp).label("newest_candle"),
        func.count(Candle.id).label("count")
    ).group_by(Candle.exchange, Candle.symbol, Candle.timeframe).all()

    return [
        {
            "exchange": r.exchange,
            "symbol": r.symbol,
            "timeframe": r.timeframe,
            "oldest_candle": r.oldest_candle,
            "newest_candle": r.newest_candle,
            "count": r.count,
        }
        for r in summary
    ]


@router.delete("")
def delete_data(
    symbol: str,
    exchange: Optional[str] = None,
    timeframe: Optional[str] = None,
    before_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    formatted_symbol = symbol.replace('-', '/').upper()
    where = "symbol = :symbol"
    params = {"symbol": formatted_symbol}

    if exchange:
        where += " AND exchange = :exchange"
        params["exchange"] = exchange.lower()
    if timeframe:
        where += " AND timeframe = :timeframe"
        params["timeframe"] = timeframe
    if before_date:
        try:
            date_limit = datetime.fromisoformat(before_date.replace('Z', '+00:00'))
            where += " AND timestamp < :before"
            params["before"] = date_limit
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use ISO 8601.")

    # Delete in chunks with a commit per chunk so the write lock is released
    # between batches and concurrent backfill commits are not starved.
    deleted_count = 0
    while True:
        result = db.execute(
            text(f"DELETE FROM candles WHERE rowid IN (SELECT rowid FROM candles WHERE {where} LIMIT 20000)"),
            params,
        )
        db.commit()
        if result.rowcount <= 0:
            break
        deleted_count += result.rowcount

    return {"message": f"Deleted {deleted_count} historical candles.", "deleted": deleted_count}


@router.get("/candles/{symbol}")
def get_candles(
    symbol: str,
    limit: int = Query(default=50000, le=100000),
    exchange: Optional[str] = None,
    x_timeframe: str = Header(...),
    db: Session = Depends(get_db)
):
    if limit <= 0:
        limit = 50000
    formatted_symbol = symbol.replace('-', '/').upper()
    # Use raw column query (no ORM object hydration) for speed with large datasets;
    # fetch the most recent N candles (DESC then reverse)
    query = db.query(
        Candle.timestamp, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume
    ).filter(
        Candle.symbol == formatted_symbol,
        Candle.timeframe == x_timeframe,
    )
    if exchange:
        query = query.filter(Candle.exchange == exchange.lower())
    rows = query.order_by(Candle.timestamp.desc()).limit(limit).all()
    rows.reverse()

    return [
        {
            "time": int(ts.replace(tzinfo=timezone.utc).timestamp()) if ts.tzinfo is None else int(ts.timestamp()),
            "open": o, "high": h, "low": l, "close": c, "value": v,
        }
        for ts, o, h, l, c, v in rows
    ]


@router.get("/market-info/{symbol}")
def get_market_info(symbol: str, exchange: str = "okx"):
    try:
        exchange_id = exchange.lower()
        formatted_symbol = symbol.replace('-', '/').upper()
        cache_key = f"{exchange_id}:{formatted_symbol}"

        now = time.monotonic()
        cached = _ticker_cache.get(cache_key)
        if cached and (now - cached[0]) < _TICKER_TTL:
            return cached[1]

        exch = build_exchange(exchange_id)
        ticker = exch.fetch_ticker(formatted_symbol)
        result = {
            "symbol": formatted_symbol,
            "last": ticker.get('last', 0),
            "change_24h": ticker.get('percentage', 0),
            "high_24h": ticker.get('high', 0),
            "low_24h": ticker.get('low', 0),
            "vol_24h": ticker.get('baseVolume', 0),
        }
        _ticker_cache[cache_key] = (now, result)
        return result
    except Exception as e:
        logger.warning("Failed to fetch market info for '%s' on '%s': %s", symbol, exchange, type(e).__name__)
        raise HTTPException(status_code=400, detail="Failed to fetch market info from the exchange.")
