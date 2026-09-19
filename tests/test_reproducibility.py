"""Backtest reproducibility: pinned window, raw-candle data hash and the
two-tier variant counter (distinct configs on this slice / ever)."""
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import text

from backend.engine.bot_manager import BotManager
from backend.engine.sizing import backtest_pin, data_fingerprint
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.orders import Order
from tests.conftest import _tf_seconds, insert_candles, make_candles

EXCHANGE, TF, SYMBOL = "binance", "1h", "BTC/USDT"
BOT = "repro-bot"
N_CANDLES = 400


def _settings(**overrides):
    base = {
        "symbols": [SYMBOL], "timeframe": TF, "data_exchange": EXCHANGE,
        "api_execution": False, "api_key_name": None,
        "backtest_on_start": True, "backtest_lookback": 300, "backtest_capital": 1000,
        "max_positions": 1, "max_positions_scope": "per_pair",
        "trade_settings": {
            "entry": {"order_type": "market", "amount_type": "percentage", "amount_value": 40,
                      "fee": 0.1, "slippage": 0,
                      "stop_losses": [{"type": "percentage", "value": 3, "close_amount_type": "percentage", "close_amount_value": 100}],
                      "take_profits": [{"type": "percentage", "value": 3, "close_amount_type": "percentage", "close_amount_value": 100}]},
            "exit": {"order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.1, "slippage": 0},
        },
        "nodes": {"entry": {"class": "condition", "left": "close", "operator": ">", "right": 0}},
        "entry_node": "entry",
        "exit_node": None,
    }
    base.update(overrides)
    return base


def _seed_rows(db):
    return insert_candles(db, make_candles(EXCHANGE, SYMBOL, TF, N_CANDLES, seed=7))


def _seed(db):
    _seed_rows(db)


def _run(db, bot):
    bot.is_active = True
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    bot = db.get(BotConfig, bot.id)
    return bot, bot.settings["last_backtest_summary"]


def _orders(db):
    return [(o.timestamp, o.side, round(o.price, 8), round(o.amount, 8)) for o in
            db.query(Order).filter(Order.bot_name == BOT, Order.mode == "backtest").order_by(Order.timestamp, Order.side, Order.id)]


def _set(db, bot, **settings):
    bot.settings = {**bot.settings, **settings}
    db.commit()
    return db.get(BotConfig, bot.id)


def _new_bot(db, **overrides):
    bot = BotConfig(name=BOT, is_active=False, is_sandbox=True, strategy="node_graph", settings=_settings(**overrides))
    db.add(bot)
    db.commit()
    return bot


# ── data hash ────────────────────────────────────────────────────────────────

def test_data_fingerprint_is_deterministic_and_sensitive():
    rows = make_candles(EXCHANGE, SYMBOL, TF, 50, seed=3)
    df = pd.DataFrame([{"timestamp": r.timestamp, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume} for r in rows])
    assert data_fingerprint(df) == data_fingerprint(df.sample(frac=1, random_state=1)), "row order must not matter"
    altered = df.copy()
    altered.loc[10, "close"] += 0.0001
    assert data_fingerprint(altered) != data_fingerprint(df)
    assert data_fingerprint(df.iloc[:-1]) != data_fingerprint(df)


def test_backtest_pin_parsing():
    assert backtest_pin({}) == (None, None)
    assert backtest_pin({"backtest_from": "2024-01-01T00:00:00"}) == (None, None)
    assert backtest_pin({"backtest_from": "2024-02-01T00:00:00", "backtest_to": "2024-01-01T00:00:00"}) == (None, None)
    f, t = backtest_pin({"backtest_from": "2024-01-01T00:00:00Z", "backtest_to": "2024-01-02T00:00:00+00:00"})
    assert f.tzinfo is None and t.tzinfo is None and (t - f) == timedelta(days=1)


# ── pinned window ────────────────────────────────────────────────────────────

def test_pinned_window_replays_the_same_run_after_new_candles_arrive(db):
    _seed(db)
    bot = _new_bot(db)
    bot, first = _run(db, bot)
    assert first["pinned"] is False
    first_orders = _orders(db)
    assert first_orders

    # Pin to the walked range, then let the market move on
    bot = _set(db, bot, backtest_from=first["data_from"], backtest_to=first["data_to"])
    last_ts = db.query(Candle.timestamp).filter(Candle.symbol == SYMBOL).order_by(Candle.timestamp.desc()).first()[0]
    extra = make_candles(EXCHANGE, SYMBOL, TF, 40, seed=99, start=last_ts + timedelta(seconds=_tf_seconds(TF)))
    insert_candles(db, extra)

    bot, second = _run(db, bot)
    assert second["pinned"] is True
    assert (second["data_from"], second["data_to"]) == (first["data_from"], first["data_to"])
    assert second["data_hash"] == first["data_hash"]
    assert second["data_changed"] is False
    assert _orders(db) == first_orders

    # Unpinned rerun ("rerun against latest") walks the newest candles again
    bot = _set(db, bot, backtest_from=None, backtest_to=None)
    bot, third = _run(db, bot)
    assert third["pinned"] is False
    assert third["data_to"] > first["data_to"]
    assert third["data_changed"] is None, "slice moved — hashes are not comparable"


def test_altered_candle_on_the_same_slice_is_flagged(db):
    _seed(db)
    bot = _new_bot(db)
    bot, first = _run(db, bot)
    bot = _set(db, bot, backtest_from=first["data_from"], backtest_to=first["data_to"])

    # Simulate a re-download / exchange restatement of one candle
    row = db.query(Candle).filter(Candle.symbol == SYMBOL, Candle.timestamp == datetime.fromisoformat(first["data_from"])).one()
    row.close = row.close * 1.001
    db.commit()

    bot, second = _run(db, bot)
    assert second["data_hash"] != first["data_hash"]
    assert second["data_changed"] is True


# ── verify against exchange ──────────────────────────────────────────────────

class ExchangeReplay:
    """fetch_ohlcv over an in-memory list of candles, optionally restated."""
    def __init__(self, rows, restate=None, page=50):
        self.rows = sorted(rows, key=lambda r: r.timestamp)
        self.restate = restate or {}   # timestamp -> {field: value}
        self.page = page               # exchange-side page cap, forces pagination
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=500):
        self.calls += 1
        out = []
        for r in self.rows:
            ms = int(r.timestamp.replace(tzinfo=timezone.utc).timestamp() * 1000)
            if since is not None and ms < since:
                continue
            vals = {f: getattr(r, f) for f in ("open", "high", "low", "close", "volume")}
            vals.update(self.restate.get(r.timestamp, {}))
            out.append([ms, vals["open"], vals["high"], vals["low"], vals["close"], vals["volume"]])
            if len(out) >= min(limit, self.page):
                break
        return out


def _candle_at(db, ts):
    return db.query(Candle).filter(Candle.symbol == SYMBOL, Candle.timestamp == ts).one()


def test_verify_window_reports_and_optionally_accepts_restated_candles(db, monkeypatch):
    import backend.engine.data_verify as dv
    monkeypatch.setattr(dv.time, "sleep", lambda *_: None)
    rows = insert_candles(db, make_candles(EXCHANGE, SYMBOL, TF, 120, seed=5))
    restate = {rows[10].timestamp: {"volume": rows[10].volume + 1.0}, rows[50].timestamp: {"close": rows[50].close * 1.01}}
    exch = ExchangeReplay(rows, restate)

    res = dv.verify_window(db, exch, EXCHANGE, SYMBOL, TF, rows[0].timestamp, rows[-1].timestamp, accept=False)
    assert res["checked"] == 120 and res["local_rows"] == 120
    assert res["restated_count"] == 2 and res["accepted"] == 0
    assert [r["timestamp"] for r in res["restated"]] == [rows[10].timestamp.isoformat(), rows[50].timestamp.isoformat()]
    assert set(res["restated"][0]["fields"]) == {"volume"}
    assert exch.calls > 1, "window must be paginated"
    db.expire_all()
    assert _candle_at(db, rows[50].timestamp).close == rows[50].close, "keep local snapshot by default"

    res = dv.verify_window(db, exch, EXCHANGE, SYMBOL, TF, rows[0].timestamp, rows[-1].timestamp, accept=True)
    assert res["accepted"] == 2
    db.expire_all()
    row = _candle_at(db, rows[50].timestamp)
    assert row.close == rows[50].close * 1.01 and row.fetched_at is not None

    # Clean now: nothing restated any more
    res = dv.verify_window(db, exch, EXCHANGE, SYMBOL, TF, rows[0].timestamp, rows[-1].timestamp)
    assert res["restated_count"] == 0


def test_verify_data_route_uses_the_last_window_and_updates_the_summary(db, monkeypatch):
    import os
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.routers import bots as bots_router
    import backend.engine.data_verify as dv
    monkeypatch.setattr(dv.time, "sleep", lambda *_: None)
    rows = _seed_rows(db)
    bot = _new_bot(db)
    bot, summary = _run(db, bot)
    exch = ExchangeReplay(rows, {rows[-5].timestamp: {"high": rows[-5].high * 1.02}})
    monkeypatch.setattr(bots_router, "build_exchange", lambda *_a, **_k: exch)
    client = TestClient(app)
    headers = {"X-API-Key": os.environ["MASTER_API_KEY"]}

    r = client.post(f"/api/bots/{bot.id}/verify-data", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exchange"] == EXCHANGE and body["restated"] == 1 and body["accepted"] == 0
    assert body["from"] == summary["data_from"] and body["to"] == summary["data_to"]
    db.expire_all()
    sm = db.get(BotConfig, bot.id).settings["last_backtest_summary"]
    assert sm["restated_candles"] == 1 and sm["verified_at"]

    r = client.post(f"/api/bots/{bot.id}/verify-data?accept=true", headers=headers)
    assert r.status_code == 200 and r.json()["accepted"] == 1
    db.expire_all()
    assert db.get(BotConfig, bot.id).settings["last_backtest_summary"]["restated_candles"] == 0

    # The accepted restatement changes the slice's data hash on the next run
    bot = _set(db, db.get(BotConfig, bot.id), backtest_from=summary["data_from"], backtest_to=summary["data_to"])
    bot, again = _run(db, bot)
    assert again["data_changed"] is True


# ── migration ────────────────────────────────────────────────────────────────

def test_bot_config_runs_migration_keeps_old_rows_as_distinct_configs(db):
    from sqlalchemy import inspect
    from backend.core.database import engine, run_migrations
    db.add(BotConfig(name="a", is_active=False, is_sandbox=True, strategy="node_graph", settings={}))
    db.commit()  # the orphan sweep after migrations drops rows of unknown bots
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE bot_config_runs"))
        conn.execute(text("CREATE TABLE bot_config_runs (id INTEGER PRIMARY KEY, bot_name VARCHAR NOT NULL, "
                          "config_hash VARCHAR(32) NOT NULL, first_run_at DATETIME NOT NULL, "
                          "CONSTRAINT uq_bot_config_runs_bot_hash UNIQUE (bot_name, config_hash))"))
        conn.execute(text("INSERT INTO bot_config_runs (bot_name, config_hash, first_run_at) VALUES "
                          "('a', 'h1', '2026-01-01 00:00:00'), ('a', 'h2', '2026-01-02 00:00:00')"))
    run_migrations()
    cols = {c["name"] for c in inspect(engine).get_columns("bot_config_runs")}
    assert {"slice_key", "window_from", "window_to", "data_hash", "run_at"} <= cols
    assert "first_run_at" not in cols
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT bot_name, config_hash, slice_key, data_hash, run_at FROM bot_config_runs ORDER BY id")).fetchall()
    assert [tuple(r) for r in rows] == [("a", "h1", "", "", "2026-01-01 00:00:00"), ("a", "h2", "", "", "2026-01-02 00:00:00")]
    run_migrations()  # idempotent


# ── variant counter ──────────────────────────────────────────────────────────

def test_two_tier_variant_counter(db):
    _seed(db)
    insert_candles(db, make_candles(EXCHANGE, "ETH/USDT", TF, N_CANDLES, seed=8, start_price=200.0))
    bot = _new_bot(db)
    bot, s1 = _run(db, bot)
    assert (s1["variants"], s1["variants_on_slice"]) == (1, 1)
    bot = _set(db, bot, backtest_from=s1["data_from"], backtest_to=s1["data_to"])

    # Same slice, unchanged data, same config → no new variant
    bot, s2 = _run(db, bot)
    assert (s2["variants"], s2["variants_on_slice"]) == (1, 1)

    # Same slice, tweaked strategy → variant #2 on this slice, 2 in total
    bot = _set(db, bot, max_positions=2)
    bot, s3 = _run(db, bot)
    assert (s3["variants"], s3["variants_on_slice"]) == (2, 2)

    # Other pair (new slice) → slice counter restarts, total keeps counting
    bot = _set(db, bot, symbols=["ETH/USDT"])
    bot, s4 = _run(db, bot)
    assert s4["slice_key"] != s3["slice_key"]
    assert (s4["variants"], s4["variants_on_slice"]) == (3, 1)

    # Layout / routing / pin changes are not strategy variants
    bot = _set(db, bot, ui_layout={"x": 1}, live_allocation_pct=50)
    bot, s5 = _run(db, bot)
    assert (s5["variants"], s5["variants_on_slice"]) == (3, 1)


def test_cache_wipe_resets_both_counters(db):
    _seed(db)
    bot = _new_bot(db)
    bot, s1 = _run(db, bot)
    bot = _set(db, bot, max_positions=2)
    bot, s2 = _run(db, bot)
    assert s2["variants"] == 2
    db.execute(text("DELETE FROM bot_config_runs WHERE bot_name = :bn"), {"bn": BOT})
    db.commit()
    bot, s3 = _run(db, bot)
    assert (s3["variants"], s3["variants_on_slice"]) == (1, 1)
