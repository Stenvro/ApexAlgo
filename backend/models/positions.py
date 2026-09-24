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
    triggered_exits = Column(JSON, nullable=True, default=list)

    orders = relationship("Order", back_populates="position", cascade="all, delete-orphan")
