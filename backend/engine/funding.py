"""Funding-rate simulation for perpetual swaps.

Every funding settlement of a perpetual moves money between longs and
shorts: at each settlement a position pays ``rate * notional`` (a long pays
when the rate is positive, a short receives it; negative rates reverse the
flow). The backtest and the forward test book these payments on their open
positions from the stored ``funding_rates`` rows; paper/live positions are
charged by the exchange itself and are not booked here.

Data path: ``ensure_history`` fetches what the exchange still has
(``fetch_funding_rate_history``, paginated) for the part of the window that
is not stored yet, ``load`` returns the sorted events of a window, and
``settlements`` picks the ones a position has not been charged for
(``funding_until < ts <= candle_ts``). Stored rows are never overwritten.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.engine.sizing import _naive_utc
from backend.models.market_data import FundingRate

logger = logging.getLogger("apexalgo.funding")

_PAGE = 1000
_MAX_PAGES = 200  # safety valve on the pagination loop
# The poller refreshes a symbol's funding at most this often (seconds)
REFRESH_EVERY = 3600
_last_refresh: dict[tuple, float] = {}


def _ms(ts) -> int:
    ts = _naive_utc(ts)
    return int(ts.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _from_ms(ms) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None)


def fetch_history(exchange_id: str, symbol: str, since_ms: int, until_ms: int) -> list:
    """`[(timestamp_ms, rate)]` from the exchange for `[since_ms, until_ms]`.
    Exchanges keep a limited history (OKX ~3 months, Binance years); what
    they no longer serve is simply not returned. Raises on network errors."""
    from backend.core.exchange_registry import build_exchange_for_symbol
    exchange = build_exchange_for_symbol(exchange_id, symbol)
    if not exchange.has.get("fetchFundingRateHistory"):
        return []
    exchange.load_markets()
    out = []
    since = int(since_ms)
    seen = set()
    for _ in range(_MAX_PAGES):
        rows = exchange.fetch_funding_rate_history(symbol, since=since, limit=_PAGE) or []
        fresh = []
        for r in rows:
            ts = r.get("timestamp")
            rate = r.get("fundingRate")
            if ts is None or rate is None or ts in seen:
                continue
            seen.add(ts)
            fresh.append((int(ts), float(rate)))
        if not fresh:
            break
        fresh.sort()
        out.extend(t for t in fresh if since_ms <= t[0] <= until_ms)
        last = fresh[-1][0]
        if last >= until_ms or len(rows) < 2 or last <= since:
            break
        since = last + 1
    out.sort()
    return out


def store(db, exchange_id: str, symbol: str, rows: list) -> int:
    """Insert `[(timestamp_ms, rate)]`, ignoring settlements already stored."""
    if not rows:
        return 0
    now = _naive_utc(datetime.now(timezone.utc))
    n = 0
    for ts_ms, rate in rows:
        res = db.execute(text(
            "INSERT OR IGNORE INTO funding_rates (exchange, symbol, timestamp, rate, fetched_at) "
            "VALUES (:ex, :sym, :ts, :rate, :now)"
        ), {"ex": exchange_id, "sym": symbol, "ts": _from_ms(ts_ms), "rate": float(rate), "now": now})
        n += res.rowcount or 0
    db.commit()
    return n


def stored_range(db, exchange_id: str, symbol: str):
    """(first, last, count) of the stored settlements for the symbol."""
    from sqlalchemy import func
    row = db.query(func.min(FundingRate.timestamp), func.max(FundingRate.timestamp), func.count(FundingRate.id)).filter(
        FundingRate.exchange == exchange_id, FundingRate.symbol == symbol).first()
    return (row[0], row[1], int(row[2] or 0)) if row else (None, None, 0)


def ensure_history(db, exchange_id: str, symbol: str, start, end) -> int:
    """Fetch and store the settlements of `[start, end]` that are not stored
    yet (only the head and/or tail beyond the stored range are requested).
    Returns the number of new rows; raises on exchange errors so the caller
    can report them."""
    start, end = _naive_utc(start), _naive_utc(end)
    first, last, _ = stored_range(db, exchange_id, symbol)
    new = 0
    if first is None or last is None:
        new += store(db, exchange_id, symbol, fetch_history(exchange_id, symbol, _ms(start), _ms(end)))
        return new
    if start < first:
        new += store(db, exchange_id, symbol, fetch_history(exchange_id, symbol, _ms(start), _ms(first) - 1))
    if end > last:
        new += store(db, exchange_id, symbol, fetch_history(exchange_id, symbol, _ms(last) + 1, _ms(end)))
    return new


def refresh_recent(db, exchange_id: str, symbol: str, lookback_days: int = 7, force: bool = False) -> int:
    """Poller hook: top up the stored settlements to now, at most once per
    `REFRESH_EVERY` seconds per symbol. Errors are logged, never raised."""
    key = (exchange_id, symbol)
    now = time.monotonic()
    if not force and now - _last_refresh.get(key, -1e9) < REFRESH_EVERY:
        return 0
    _last_refresh[key] = now
    try:
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        return ensure_history(db, exchange_id, symbol, end - timedelta(days=lookback_days), end)
    except Exception as exc:
        logger.warning("Funding refresh failed for %s %s: %s", exchange_id, symbol, exc)
        return 0


def load(db, exchange_id: str, symbol: str, start=None, end=None) -> list:
    """Sorted `[(timestamp, rate)]` (naive UTC) of the stored settlements in
    `[start, end]`; the whole history when no bounds are given."""
    q = db.query(FundingRate.timestamp, FundingRate.rate).filter(
        FundingRate.exchange == exchange_id, FundingRate.symbol == symbol)
    if start is not None:
        q = q.filter(FundingRate.timestamp >= _naive_utc(start))
    if end is not None:
        q = q.filter(FundingRate.timestamp <= _naive_utc(end))
    return [(_naive_utc(ts), float(rate)) for ts, rate in q.order_by(FundingRate.timestamp.asc()).all()]


def settlements(events: list, after, until) -> list:
    """The events with `after < ts <= until` (both naive UTC)."""
    if not events:
        return []
    after = _naive_utc(after) if after is not None else None
    until = _naive_utc(until)
    return [(ts, r) for ts, r in events if (after is None or ts > after) and ts <= until]


def payment(spec, side, qty, price, rate) -> float:
    """Cash flow of one settlement for a position of `qty` at `price`:
    ``-direction * rate * notional`` — positive when the position receives
    funding. A long pays a positive rate, a short receives it."""
    from backend.engine.pnl import direction
    return -direction(side) * float(rate) * spec.notional_cash(qty, price)


def coverage(events: list, start, end) -> dict:
    """What the stored settlements cover of a backtest window — reported in
    the summary so a run on a partial funding history is recognisable."""
    if not events:
        return {"funding": "no data", "funding_events": 0}
    first, last = events[0][0], events[-1][0]
    return {
        "funding": "simulated",
        "funding_events": len(events),
        "funding_from": first.isoformat(),
        "funding_to": last.isoformat(),
    }
