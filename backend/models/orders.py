from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from backend.core.database import Base

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)

    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True, index=True)
    exchange = Column(String, index=True, default="okx")  # e.g. "okx", "binance"
    bot_name = Column(String, index=True)

    # CRUCIAAL VOOR BELASTING/BOEKHOUDING: 'live', 'paper', of 'backtest'
    mode = Column(String, default="paper", index=True)

    exchange_order_id = Column(String, nullable=True)
    symbol = Column(String, index=True)
    side = Column(String)                               # "buy" of "sell"
    order_type = Column(String)                         # "limit" of "market"

    price = Column(Float)
    amount = Column(Float)
    fee = Column(Float, nullable=True)                  # in the position's cash currency
    fee_currency = Column(String, nullable=True)        # what the exchange charged it in (live), when known
    fee_cash = Column(Float, nullable=True)             # the raw charged amount in `fee_currency`

    status = Column(String, default="open")             # "open", "filled", "canceled", "rejected"
    market_type = Column(String, default="spot")        # "spot" | "swap"
    reduce_only = Column(Integer, default=0)            # 1 for derivative close orders

    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    position = relationship("Position", back_populates="orders")
