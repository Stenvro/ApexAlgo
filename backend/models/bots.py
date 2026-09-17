from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON
from datetime import datetime, timezone
from backend.core.database import Base

class BotConfig(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    is_active = Column(Boolean, default=False)
    is_sandbox = Column(Boolean, default=True)
    strategy = Column(String)

    settings = Column(JSON, default=dict)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
