import logging
import os
from dotenv import load_dotenv, find_dotenv
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv(find_dotenv())

logger = logging.getLogger("apexalgo.database")

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/ApexAlgoDB.sqlite3")

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30}
)

@event.listens_for(engine, "connect")
def set_sqlite_pragmas(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")       # Allow concurrent reads during writes
    cursor.execute("PRAGMA synchronous=NORMAL")     # Faster writes, still crash-safe
    cursor.execute("PRAGMA cache_size=-32000")      # 32 MB page cache
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def run_migrations():
    """
    Idempotent schema migrations for columns that SQLAlchemy create_all() cannot add
    to pre-existing tables, and for the Candle table unique-constraint upgrade.
    """
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    with engine.connect() as conn:

        # ── positions ────────────────────────────────────────────────────────────
        if "positions" in existing_tables:
            pos_cols = {c['name'] for c in inspector.get_columns('positions')}
            if 'highest_price' not in pos_cols:
                conn.execute(text("ALTER TABLE positions ADD COLUMN highest_price REAL"))
                logger.info("Migration: added 'highest_price' to positions")
            if 'triggered_exits' not in pos_cols:
                conn.execute(text("ALTER TABLE positions ADD COLUMN triggered_exits JSON"))
                logger.info("Migration: added 'triggered_exits' to positions")
            if 'exchange' not in pos_cols:
                conn.execute(text("ALTER TABLE positions ADD COLUMN exchange TEXT DEFAULT 'okx'"))
                logger.info("Migration: added 'exchange' to positions")

        # ── orders ───────────────────────────────────────────────────────────────
        if "orders" in existing_tables:
            ord_cols = {c['name'] for c in inspector.get_columns('orders')}
            if 'exchange' not in ord_cols:
                conn.execute(text("ALTER TABLE orders ADD COLUMN exchange TEXT DEFAULT 'okx'"))
                logger.info("Migration: added 'exchange' to orders")

        # ── candles ──────────────────────────────────────────────────────────────
        # The unique constraint must include 'exchange'. SQLite cannot alter
        # constraints in-place, so we recreate the table when needed.
        if "candles" in existing_tables:
            can_cols = {c['name'] for c in inspector.get_columns('candles')}
            if 'exchange' not in can_cols:
                logger.info("Migration: rebuilding candles table to add 'exchange' column...")
                conn.execute(text("""
                    CREATE TABLE candles_migration (
                        id        INTEGER PRIMARY KEY,
                        exchange  TEXT    NOT NULL DEFAULT 'okx',
                        symbol    TEXT,
                        timeframe TEXT,
                        timestamp DATETIME,
                        open      REAL,
                        high      REAL,
                        low       REAL,
                        close     REAL,
                        volume    REAL,
                        marketcap REAL,
                        UNIQUE (exchange, symbol, timeframe, timestamp)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO candles_migration
                        (id, exchange, symbol, timeframe, timestamp,
                         open, high, low, close, volume, marketcap)
                    SELECT
                        id, 'okx', symbol, timeframe, timestamp,
                        open, high, low, close, volume, marketcap
                    FROM candles
                """))
                conn.execute(text("DROP TABLE candles"))
                conn.execute(text("ALTER TABLE candles_migration RENAME TO candles"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candles_exchange  ON candles (exchange)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candles_symbol    ON candles (symbol)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candles_timeframe ON candles (timeframe)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candles_timestamp ON candles (timestamp)"))
                logger.info("Migration: candles table rebuilt successfully.")

        # ── signals — deduplicate and add unique constraint ────────────────────
        if "signals" in existing_tables:
            # Check if the unique constraint already exists by looking for the index
            # Check if the unique constraint already exists (works for both named and auto-generated SQLite indexes)
            existing_idx_rows = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'")).fetchone()
            has_unique = existing_idx_rows and 'UNIQUE' in (existing_idx_rows[0] or '').upper()
            if not has_unique:
                logger.info("Migration: rebuilding signals table to add unique constraint...")
                # Deduplicate: keep the row with the highest id per (bot_name, symbol, timestamp)
                conn.execute(text("""
                    DELETE FROM signals WHERE id NOT IN (
                        SELECT MAX(id) FROM signals GROUP BY bot_name, symbol, timestamp
                    )
                """))
                # Rebuild table with unique constraint
                conn.execute(text("""
                    CREATE TABLE signals_migration (
                        id          INTEGER PRIMARY KEY,
                        candle_id   INTEGER REFERENCES candles(id) ON DELETE CASCADE,
                        symbol      TEXT,
                        timestamp   DATETIME,
                        bot_name    TEXT,
                        name        TEXT,
                        value       REAL,
                        action      TEXT,
                        extra_data  JSON DEFAULT '{}',
                        UNIQUE (bot_name, symbol, timestamp)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO signals_migration
                        (id, candle_id, symbol, timestamp, bot_name, name, value, action, extra_data)
                    SELECT id, candle_id, symbol, timestamp, bot_name, name, value, action, extra_data
                    FROM signals
                """))
                conn.execute(text("DROP TABLE signals"))
                conn.execute(text("ALTER TABLE signals_migration RENAME TO signals"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_signals_candle_id ON signals (candle_id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_signals_symbol    ON signals (symbol)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_signals_timestamp ON signals (timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_signals_bot_name  ON signals (bot_name)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_signals_name      ON signals (name)"))
                logger.info("Migration: signals table rebuilt with unique constraint.")

        # Composite indexes for hot query paths (idempotent)
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_positions_botname_status ON positions (bot_name, status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_posid_side_status ON orders (position_id, side, status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_signals_symbol_botname   ON signals (symbol, bot_name)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_cooldown          ON orders (bot_name, symbol, mode, side, timestamp)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candles_lookup           ON candles (exchange, symbol, timeframe, timestamp)"))
        # Composite indexes for batch position loading and signal dedup
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_positions_bot_status_mode_symbol ON positions (bot_name, status, mode, symbol)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_signals_bot_symbol_ts ON signals (bot_name, symbol, timestamp)"))

        conn.commit()

        _sweep_orphaned_bot_rows(conn, existing_tables)


def _sweep_orphaned_bot_rows(conn, existing_tables) -> None:
    """Delete per-bot rows whose bot no longer exists.

    Bot deletion cleans these tables in a background task after the response
    is sent; a restart in that window (or rows from before the cleanup covered
    every table) leaves orphans behind. Rows are keyed by bot name, so nothing
    else references them once the bot is gone.
    """
    if "bots" not in existing_tables:
        return
    for table in ("signals", "orders", "positions", "bot_logs"):
        if table not in existing_tables:
            continue
        result = conn.execute(text(
            f"DELETE FROM {table} WHERE bot_name IS NOT NULL "
            f"AND bot_name NOT IN (SELECT name FROM bots)"
        ))
        conn.commit()
        if result.rowcount > 0:
            logger.info("Cleanup: removed %d orphaned %s row(s) for deleted bots", result.rowcount, table)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()