from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from backend.core.database import Base


class BotConfigRun(Base):
    """Run log of a bot's backtests: one row per (strategy configuration,
    data slice, data hash).

    Only hashes are kept, not the config or the candles. Two counters are
    derived from it — distinct configs ever run ("42 configs total") and
    distinct configs run on exactly this slice ("variant #3 on this slice") —
    so the user can see how often a strategy was tweaked and re-run on the
    same data (overfitting awareness). The data hash lets a rerun on the same
    slice tell whether the candles underneath changed. Cleared by a cache wipe
    together with the rest of the bot's derived data.
    """
    __tablename__ = "bot_config_runs"
    __table_args__ = (UniqueConstraint("bot_name", "config_hash", "slice_key", "data_hash", name="uq_bot_config_runs_run"),)

    id           = Column(Integer, primary_key=True, autoincrement=True)
    bot_name     = Column(String, nullable=False, index=True)
    config_hash  = Column(String(32), nullable=False)
    slice_key    = Column(String(32), nullable=False, default="")   # md5(exchange, symbols, timeframe, window)
    window_from  = Column(DateTime, nullable=True)
    window_to    = Column(DateTime, nullable=True)
    data_hash    = Column(String(64), nullable=False, default="")   # sha256 of the raw candles walked
    run_at       = Column(DateTime, nullable=False)
