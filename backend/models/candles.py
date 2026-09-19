from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from backend.core.database import Base


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Candle(Base):
    __tablename__ = "candles"

    id = Column(Integer, primary_key=True, index=True)
    exchange = Column(String, index=True, default="okx")  # e.g. "okx", "binance"
    symbol = Column(String, index=True)                   # e.g. "BTC/USDT"
    timeframe = Column(String, index=True)                # e.g. "1m", "15m", "1h"
    timestamp = Column(DateTime, index=True)

    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    marketcap = Column(Float)
    # When this row was written (or last overwritten by an accepted exchange
    # restatement). NULL on rows that predate the column. Existing rows are
    # otherwise never updated, so a backtest on stored candles stays
    # reproducible until the user re-downloads or accepts a verification.
    fetched_at = Column(DateTime, nullable=True, default=_utcnow)

    __table_args__ = (
        UniqueConstraint('exchange', 'symbol', 'timeframe', 'timestamp', name='_ex_symbol_tf_ts_uc'),
    )

    signals = relationship("Signal", back_populates="candle", cascade="all, delete-orphan", passive_deletes=True)
