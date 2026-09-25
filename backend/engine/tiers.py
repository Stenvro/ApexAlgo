"""Tiered maintenance margin for perpetual swaps.

Exchanges keep a larger maintenance margin for larger positions: a list of
brackets (``fetch_market_leverage_tiers``) each with a size range, a
maintenance-margin rate and a leverage cap. The liquidation price of a
position depends on the rate of its bracket, so the simulation looks its
rate up here instead of using the flat ``MAINTENANCE_MARGIN`` — which
remains the fallback when the exchange serves no tiers (spot exchanges,
network errors, tests).

Size is the quote notional of the position on linear contracts and the
contract count on inverse ones (the Binance COIN-M / OKX convention ccxt
normalises the brackets to). Tiers are stored per (exchange, symbol) and
refreshed by the bot startup at most once a week.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from backend.engine.contracts import MAINTENANCE_MARGIN
from backend.engine.sizing import _naive_utc
from backend.models.market_data import LeverageTier

logger = logging.getLogger("apexalgo.tiers")

MAX_AGE = timedelta(days=7)


def fetch(exchange_id: str, symbol: str) -> list:
    """`[(tier, min_size, max_size|None, mmr, max_leverage|None)]` from the
    exchange; empty when it has no tier endpoint. Raises on network errors."""
    from backend.core.exchange_registry import build_exchange_for_symbol
    exchange = build_exchange_for_symbol(exchange_id, symbol)
    if not exchange.has.get("fetchMarketLeverageTiers") and not exchange.has.get("fetchLeverageTiers"):
        return []
    exchange.load_markets()
    if exchange.has.get("fetchMarketLeverageTiers"):
        rows = exchange.fetch_market_leverage_tiers(symbol) or []
    else:
        rows = (exchange.fetch_leverage_tiers([symbol]) or {}).get(symbol) or []
    out = []
    for i, r in enumerate(rows):
        mmr = r.get("maintenanceMarginRate")
        if mmr is None:
            continue
        lo = r.get("minNotional")
        hi = r.get("maxNotional")
        lev = r.get("maxLeverage")
        out.append((int(r.get("tier") or (i + 1)), float(lo or 0.0), float(hi) if hi else None,
                    float(mmr), float(lev) if lev else None))
    out.sort(key=lambda t: t[1])
    return out


def store(db, exchange_id: str, symbol: str, rows: list) -> None:
    """Replace the stored brackets of the symbol with `rows`."""
    if not rows:
        return
    now = _naive_utc(datetime.now(timezone.utc))
    db.query(LeverageTier).filter(LeverageTier.exchange == exchange_id, LeverageTier.symbol == symbol).delete()
    for tier, lo, hi, mmr, lev in rows:
        db.add(LeverageTier(exchange=exchange_id, symbol=symbol, tier=tier, min_size=lo, max_size=hi,
                            mmr=mmr, max_leverage=lev, fetched_at=now))
    db.commit()


def ensure(db, exchange_id: str, symbol: str, max_age: timedelta = MAX_AGE) -> bool:
    """Fetch the brackets when none are stored or the stored ones are older
    than `max_age`. Returns True when tiers are available afterwards; raises
    on exchange errors (the caller decides whether that is a WARN)."""
    newest = db.query(func.max(LeverageTier.fetched_at)).filter(
        LeverageTier.exchange == exchange_id, LeverageTier.symbol == symbol).scalar()
    now = _naive_utc(datetime.now(timezone.utc))
    if newest is not None and now - _naive_utc(newest) < max_age:
        return True
    rows = fetch(exchange_id, symbol)
    store(db, exchange_id, symbol, rows)
    return bool(rows) or newest is not None


def load(db, exchange_id: str, symbol: str) -> list:
    """Sorted `[(min_size, max_size|None, mmr)]` of the stored brackets."""
    rows = db.query(LeverageTier.min_size, LeverageTier.max_size, LeverageTier.mmr).filter(
        LeverageTier.exchange == exchange_id, LeverageTier.symbol == symbol).order_by(LeverageTier.min_size.asc()).all()
    return [(float(lo or 0.0), float(hi) if hi is not None else None, float(mmr)) for lo, hi, mmr in rows]


def mmr_for(tiers: list, size: float) -> float:
    """Maintenance-margin rate of the bracket `size` falls in; the flat
    `MAINTENANCE_MARGIN` without tiers."""
    if not tiers:
        return MAINTENANCE_MARGIN
    size = float(size or 0.0)
    for lo, hi, mmr in tiers:
        if size >= lo and (hi is None or size < hi):
            return mmr
    return tiers[-1][2] if size >= tiers[-1][0] else tiers[0][2]


def tier_size(spec, qty, price) -> float:
    """The size the brackets are keyed on for `qty` of this contract."""
    return float(qty or 0.0) if spec.is_inverse else spec.notional_quote(qty, price)
