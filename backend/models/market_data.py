"""Derivatives market data next to the candles: funding rates and the
exchange's maintenance-margin tiers. Both are stored per (exchange, symbol)
like candles and never overwritten by the poller, so a backtest that ran
with them stays reproducible."""
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint
from backend.core.database import Base


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FundingRate(Base):
    """One funding settlement of a perpetual: `rate` is the fraction of the
    notional paid at `timestamp` (positive: longs pay shorts)."""
    __tablename__ = "funding_rates"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "timestamp", name="uq_funding_rates_ex_sym_ts"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    exchange = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    rate = Column(Float, nullable=False)
    fetched_at = Column(DateTime, nullable=True, default=_utcnow)


class LeverageTier(Base):
    """One maintenance-margin bracket of a perpetual (`fetch_market_leverage_tiers`):
    positions whose size lies in [min_size, max_size) keep `mmr` of their
    notional as maintenance margin. Size is the quote notional on linear
    contracts and the contract count on inverse ones (Binance/OKX convention).
    Refreshed by the bot startup at most once a week."""
    __tablename__ = "leverage_tiers"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "tier", name="uq_leverage_tiers_ex_sym_tier"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    exchange = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    tier = Column(Integer, nullable=False)
    min_size = Column(Float, nullable=False, default=0.0)
    max_size = Column(Float, nullable=True)          # None: open-ended top bracket
    mmr = Column(Float, nullable=False)
    max_leverage = Column(Float, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=_utcnow)
