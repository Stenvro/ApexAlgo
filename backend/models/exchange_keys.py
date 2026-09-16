from sqlalchemy import Column, Integer, String, Boolean, DateTime
from datetime import datetime, timezone
from backend.core.database import Base

class ExchangeKey(Base):
    __tablename__ = "exchange_keys"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    exchange = Column(String, default="okx")
    api_key = Column(String, nullable=False)
    api_secret = Column(String, nullable=False)
    passphrase = Column(String, nullable=False)
    is_sandbox = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
