from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from backend.core.database import Base


class BotConfigRun(Base):
    """One row per distinct strategy configuration a bot has backtested.

    Only the hash is kept, not the config itself: the count of rows per bot is
    shown as "variant #N" so the user can see how often a strategy was tweaked
    and re-run on the same data (overfitting awareness). Cleared by a cache
    wipe together with the rest of the bot's derived data.
    """
    __tablename__ = "bot_config_runs"
    __table_args__ = (UniqueConstraint("bot_name", "config_hash", name="uq_bot_config_runs_bot_hash"),)

    id           = Column(Integer, primary_key=True, autoincrement=True)
    bot_name     = Column(String, nullable=False, index=True)
    config_hash  = Column(String(32), nullable=False)
    first_run_at = Column(DateTime, nullable=False)
