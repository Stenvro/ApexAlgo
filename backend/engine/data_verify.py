"""Verify stored candles against the exchange.

Stored candles are never overwritten by the poller or a download (only
missing rows are added), so a backtest on them is reproducible — but an
exchange can silently restate history (Binance most often the volume). This
module re-fetches a window and diffs it against the local rows; with
``accept=True`` the restated rows are overwritten (and stamped `fetched_at`),
which is the one deliberate path that changes a slice's data hash.
"""
import logging
import math
import time
from datetime import datetime, timezone

from backend.models.candles import Candle
from backend.engine.sizing import _naive_utc

logger = logging.getLogger("apexalgo.data_verify")

FIELDS = ("open", "high", "low", "close", "volume")
REL_TOL = 1e-9
MAX_DETAIL = 50


def _differs(a, b) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    try:
        return not math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=0.0)
    except (TypeError, ValueError):
        return True


def fetch_window(exch, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int = 500, pause: float = 0.35):
    """Paginated OHLCV between two naive-UTC datetimes (inclusive), as
    ``{naive_utc_ts: (o, h, l, c, v)}``."""
    start_ms = int(start.replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(end.replace(tzinfo=timezone.utc).timestamp() * 1000)
    out = {}
    since = start_ms
    while since <= end_ms:
        batch = exch.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        if not batch:
            break
        for row in batch:
            ts_ms = int(row[0])
            if start_ms <= ts_ms <= end_ms:
                ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
                out[ts] = tuple(float(x) if x is not None else None for x in row[1:6])
        last = int(batch[-1][0])
        if last >= end_ms or len(batch) < 2 or last < since:
            break
        since = last + 1
        time.sleep(pause)
    return out


def verify_window(db, exch, exchange_id: str, symbol: str, timeframe: str, start, end, accept: bool = False) -> dict:
    """Diff the exchange's view of ``[start, end]`` against the stored rows.

    Returns counts plus up to ``MAX_DETAIL`` restated rows with per-field
    (local, exchange) pairs. ``accept`` overwrites restated local rows in
    place (same ids, so signals keep their candle reference) and commits.
    """
    start = _naive_utc(start)
    end = _naive_utc(end)
    remote = fetch_window(exch, symbol, timeframe, start, end)
    local_rows = db.query(Candle).filter(
        Candle.exchange == exchange_id, Candle.symbol == symbol, Candle.timeframe == timeframe,
        Candle.timestamp >= start, Candle.timestamp <= end,
    ).all()
    local = {_naive_utc(r.timestamp): r for r in local_rows}

    restated, missing_local = [], 0
    restated_total = accepted = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for ts, values in sorted(remote.items()):
        row = local.get(ts)
        if row is None:
            missing_local += 1
            continue
        diffs = {}
        for field, new in zip(FIELDS, values):
            old = getattr(row, field)
            if _differs(old, new):
                diffs[field] = {"local": old, "exchange": new}
        if not diffs:
            continue
        restated_total += 1
        if len(restated) < MAX_DETAIL:
            restated.append({"timestamp": ts.isoformat(), "fields": diffs})
        if accept:
            for field, d in diffs.items():
                setattr(row, field, d["exchange"])
            row.fetched_at = now
            accepted += 1
    missing_exchange = sum(1 for ts in local if ts not in remote)
    if accept and accepted:
        db.commit()
        logger.info("Accepted %d restated candle(s) from %s for %s/%s", accepted, exchange_id, symbol, timeframe)

    return {
        "exchange": exchange_id, "symbol": symbol, "timeframe": timeframe,
        "from": start.isoformat(), "to": end.isoformat(),
        "checked": len(remote), "local_rows": len(local),
        "restated_count": restated_total, "restated": restated,
        "missing_local": missing_local, "missing_exchange": missing_exchange,
        "accepted": accepted,
        "verified_at": now.isoformat(),
    }
