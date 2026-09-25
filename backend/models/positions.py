from sqlalchemy import Column, Integer, String, Float, DateTime, JSON
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from backend.core.database import Base

class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    exchange = Column(String, index=True, default="okx")  # e.g. "okx", "binance"
    bot_name = Column(String, index=True)
    symbol = Column(String, index=True)

    # CRUCIAAL VOOR BELASTING/BOEKHOUDING: 'live', 'paper', of 'backtest'
    mode = Column(String, default="paper", index=True)

    status = Column(String, default="open", index=True) # "open" of "closed"
    side = Column(String, default="long")               # "long" of "short"

    entry_price = Column(Float, nullable=True)          # Gemiddelde koopprijs
    amount = Column(Float, default=0.0)                 # Totale grootte van positie

    profit_abs = Column(Float, nullable=True)           # Winst in USDT/EUR
    profit_pct = Column(Float, nullable=True)           # Winst in procenten

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime, nullable=True)

    highest_price = Column(Float, nullable=True)
    # Derivatives: "swap" positions carry leverage (margin = entry x amount /
    # leverage) and the ccxt contract count behind `amount` (base units)
    market_type = Column(String, default="spot")
    leverage = Column(Float, default=1.0)
    contracts = Column(Float, nullable=True)
    # Currency-aware books (sprint D): the currency `profit_abs` and the
    # margin are in, and how to read `amount` back (base units on
    # spot/linear, contracts of `contract_size` quote units on inverse)
    cash_currency = Column(String, nullable=True)
    contract_kind = Column(String, nullable=True)   # "spot" | "linear" | "inverse"
    contract_size = Column(Float, nullable=True)
    # Funding (v2.3): sum of the simulated funding payments booked on this
    # position (cash currency, negative = paid; already inside profit_abs)
    # and the timestamp of the last settlement applied, so every funding
    # event is charged exactly once (backtest and forward test only)
    funding_paid = Column(Float, nullable=True)
    funding_until = Column(DateTime, nullable=True)
    triggered_exits = Column(JSON, nullable=True, default=list)

    orders = relationship("Order", back_populates="position", cascade="all, delete-orphan")
