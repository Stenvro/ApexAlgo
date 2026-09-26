"""Test harness.

The backend reads its environment at import time (``database.py`` builds the
engine from ``DATABASE_URL``; ``security.py`` / ``encryption.py`` raise without
their keys), so everything is pinned *before* ``backend`` is imported. The
database is a throw-away SQLite file per test session — the real
``data/ApexAlgoDB.sqlite3`` is never touched.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from cryptography.fernet import Fernet

_TMP_DIR = tempfile.mkdtemp(prefix="apexalgo-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test.sqlite3"
os.environ.setdefault("MASTER_API_KEY", "test-master-key-not-secret")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

# Importing main creates all tables and runs the idempotent migrations,
# exactly like a real startup does.
import backend.main  # noqa: E402,F401
from backend.core.database import Base, SessionLocal, engine  # noqa: E402
from backend.models.candles import Candle  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"


def _tf_seconds(timeframe: str) -> int:
    unit = timeframe[-1]
    n = int(timeframe[:-1])
    return n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


@pytest.fixture(autouse=True)
def clean_db():
    """Every test starts from empty tables (schema is kept)."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture(autouse=True)
def no_market_data_network(monkeypatch):
    """The bot startup fetches funding rates and margin tiers for perpetual
    symbols; tests never reach the exchange for those (they seed the tables
    themselves when a test needs them)."""
    from backend.engine import funding, tiers
    monkeypatch.setattr(funding, "fetch_history", lambda *a, **k: [])
    monkeypatch.setattr(tiers, "fetch", lambda *a, **k: [])
    monkeypatch.setattr(funding, "_last_refresh", {})
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_candles(exchange, symbol, timeframe, n, seed, start_price=100.0,
                 start=datetime(2023, 1, 1), drift=0.0005, vol=0.02):
    """Deterministic geometric random walk with internally consistent OHLC.

    Prices are rounded to 4 decimals so the golden snapshots stay readable
    and independent of float formatting.
    """
    rng = np.random.RandomState(seed)
    rets = rng.normal(drift, vol, n)
    closes = start_price * np.exp(np.cumsum(rets))
    wicks = np.abs(rng.normal(0, vol / 2, n))
    step = timedelta(seconds=_tf_seconds(timeframe))
    rows = []
    prev_close = start_price
    for i in range(n):
        o = prev_close
        c = float(closes[i])
        hi = max(o, c) * (1 + float(wicks[i]))
        lo = min(o, c) * (1 - float(wicks[i]))
        rows.append(Candle(
            exchange=exchange, symbol=symbol, timeframe=timeframe,
            timestamp=start + i * step,
            open=round(o, 4), high=round(hi, 4), low=round(lo, 4), close=round(c, 4),
            volume=round(float(rng.uniform(100, 1000)), 2),
        ))
        prev_close = c
    return rows


def insert_candles(db, rows):
    db.bulk_save_objects(rows)
    db.commit()
    return rows


def example_names():
    return sorted(p.name.replace(".apex.json", "") for p in EXAMPLES_DIR.glob("*.apex.json"))


def load_example(name):
    """Return (bot_name, settings) from ``examples/<name>.apex.json``."""
    with open(EXAMPLES_DIR / f"{name}.apex.json") as fh:
        payload = json.load(fh)
    bot = payload["bot"]
    return bot["name"], bot["settings"]
