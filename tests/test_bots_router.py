"""Router-level contract for stopping a bot that still holds real positions
(plan item 1.6). Runs through FastAPI's TestClient without the lifespan, so no
poller or engine thread is started; the exchange is the ``ExchangeMock`` from
the live-tick tests, wired into ``close_position_now`` via
``get_authenticated_exchange``."""
import os

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models.bot_logs import BotLog
from backend.models.bots import BotConfig
from backend.models.exchange_keys import ExchangeKey
from backend.models.orders import Order
from backend.models.positions import Position
from backend.routers import trades as trades_router
from tests.conftest import insert_candles, make_candles
from tests.test_live_tick import EXCHANGE, KEY_NAME, SYMBOL, TF, ExchangeMock, _settings

HEADERS = {"X-API-Key": os.environ["MASTER_API_KEY"]}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def running_bot(db):
    def _make(name="live-bot", positions=()):
        candles = make_candles(EXCHANGE, SYMBOL, TF, 30, seed=3, start_price=100.0)
        if not db.query(ExchangeKey).filter(ExchangeKey.name == KEY_NAME).first():
            # First bot of the test: shared candles + key
            insert_candles(db, candles)
            db.add(ExchangeKey(name=KEY_NAME, exchange=EXCHANGE, api_key="x", api_secret="y", passphrase="", is_sandbox=False))
        bot = BotConfig(name=name, is_active=True, is_sandbox=False, strategy="node_graph", settings=_settings())
        db.add(bot)
        db.flush()
        for mode, symbol, amount in positions:
            pos = Position(exchange=EXCHANGE, bot_name=name, symbol=symbol, mode=mode, status="open",
                           side="long", entry_price=100.0, amount=amount, created_at=candles[-2].timestamp)
            db.add(pos)
            db.flush()
            db.add(Order(position_id=pos.id, exchange=EXCHANGE, bot_name=name, mode=mode, symbol=symbol,
                         side="buy", order_type="market", price=100.0, amount=amount, fee=0.0, status="filled",
                         timestamp=candles[-2].timestamp, exchange_order_id="seed"))
        db.commit()
        return bot
    return _make


def _is_active(db, bot_id):
    db.expire_all()
    return db.query(BotConfig.is_active).filter(BotConfig.id == bot_id).scalar()


def _logs(db, name, level):
    return [l.msg for l in db.query(BotLog).filter(BotLog.bot_name == name, BotLog.level == level).all()]


def test_stop_without_positions_just_stops(db, client, running_bot):
    bot = running_bot(positions=[("backtest", SYMBOL, 1.0)])
    r = client.post(f"/api/bots/{bot.id}/stop", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["closed_positions"] == [] and r.json()["unmanaged_positions"] == []
    assert _is_active(db, bot.id) is False


def test_stop_with_open_real_positions_is_refused_until_decided(db, client, running_bot):
    bot = running_bot(positions=[("live", SYMBOL, 0.5), ("forward_test", "ETH/USDT", 2.0)])
    r = client.post(f"/api/bots/{bot.id}/stop", headers=HEADERS)
    assert r.status_code == 409, r.text
    listed = r.json()["detail"]["open_positions"]
    assert [(p["mode"], p["symbol"], p["amount"]) for p in listed] == [("live", SYMBOL, 0.5), ("forward_test", "ETH/USDT", 2.0)]
    assert _is_active(db, bot.id) is True  # nothing changed


def test_stop_close_positions_true_closes_at_market_then_stops(db, client, running_bot, monkeypatch):
    bot = running_bot(positions=[("live", SYMBOL, 0.5), ("forward_test", SYMBOL, 2.0)])
    mock = ExchangeMock(average=110.0)
    monkeypatch.setattr(trades_router, "get_authenticated_exchange", lambda key, **kw: mock)
    r = client.post(f"/api/bots/{bot.id}/stop", params={"close_positions": "true"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert len(r.json()["closed_positions"]) == 2
    # Exactly one real market sell (the forward-test leg closes at the last candle)
    assert [(o["side"], o["symbol"], o["amount"]) for o in mock.created] == [("sell", SYMBOL, 0.5)]
    db.expire_all()
    assert db.query(Position).filter(Position.bot_name == bot.name, Position.status == "open").count() == 0
    live = db.query(Position).filter(Position.bot_name == bot.name, Position.mode == "live").one()
    assert live.profit_abs == pytest.approx((110.0 - 100.0) * 0.5)
    assert _is_active(db, bot.id) is False


def test_stop_close_positions_false_leaves_them_unmanaged_with_warning(db, client, running_bot):
    bot = running_bot(positions=[("live", SYMBOL, 0.5)])
    r = client.post(f"/api/bots/{bot.id}/stop", params={"close_positions": "false"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert [p["id"] for p in r.json()["unmanaged_positions"]]
    assert _is_active(db, bot.id) is False
    assert db.query(Position).filter(Position.bot_name == bot.name, Position.status == "open").count() == 1
    warns = _logs(db, bot.name, "WARN")
    assert any("unmanaged" in w and SYMBOL in w for w in warns), warns


def test_stop_reports_failed_close_but_bot_is_stopped(db, client, running_bot, monkeypatch):
    bot = running_bot(positions=[("live", SYMBOL, 0.5)])

    def boom(key, **kw):
        raise RuntimeError("exchange down")
    monkeypatch.setattr(trades_router, "get_authenticated_exchange", boom)
    r = client.post(f"/api/bots/{bot.id}/stop", params={"close_positions": "true"}, headers=HEADERS)
    assert r.status_code >= 400, r.text
    detail = r.json()["detail"]
    assert "stopped" in detail["message"] and detail["open_positions"][0]["symbol"] == SYMBOL
    assert _is_active(db, bot.id) is False
    db.expire_all()
    assert db.query(Position).filter(Position.bot_name == bot.name, Position.status == "open").count() == 1
    assert _logs(db, bot.name, "ERROR")


def test_restart_keeps_positions_managed_without_asking(db, client, running_bot):
    bot = running_bot(positions=[("live", SYMBOL, 0.5)])
    r = client.post(f"/api/bots/{bot.id}/restart", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert _is_active(db, bot.id) is True
    assert not _logs(db, bot.name, "WARN")


def test_bulk_stop_lists_positions_per_bot_and_honours_flag(db, client, running_bot):
    a = running_bot(name="bot-a", positions=[("paper", SYMBOL, 0.3)])
    b = running_bot(name="bot-b")
    r = client.post("/api/bots/bulk/stop", headers=HEADERS)
    assert r.status_code == 409, r.text
    assert list(r.json()["detail"]["bots"]) == ["bot-a"]
    assert _is_active(db, a.id) is True and _is_active(db, b.id) is True

    r = client.post("/api/bots/bulk/stop", params={"close_positions": "false"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["stopped"]) == ["bot-a", "bot-b"]
    assert [p["bot_name"] for p in r.json()["unmanaged_positions"]] == ["bot-a"]
    assert _is_active(db, a.id) is False and _is_active(db, b.id) is False


# ── 1.7 router guard rails ─────────────────────────────────────────────────

def test_key_delete_refused_while_a_bot_references_it(db, client, running_bot):
    bot = running_bot()
    r = client.delete(f"/api/keys/{KEY_NAME}", headers=HEADERS)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["bots"] == [{"name": bot.name, "is_active": True, "live": True}]
    assert db.query(ExchangeKey).filter(ExchangeKey.name == KEY_NAME).count() == 1

    # Unlink → delete goes through
    bot.settings = {**bot.settings, "api_key_name": None, "api_execution": False}
    bot.is_active = False
    db.commit()
    r = client.delete(f"/api/keys/{KEY_NAME}", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert db.query(ExchangeKey).filter(ExchangeKey.name == KEY_NAME).count() == 0


def test_update_flushes_signals_only_when_strategy_changes(db, client, running_bot):
    from datetime import datetime

    from backend.models.signals import Signal
    bot = running_bot(positions=[("backtest", SYMBOL, 1.0)])
    bot.is_active = False
    # A live bot must carry a max_order_value; it is a strategy variant now
    # (caps the backtest too), so the layout-only save below leaves it as is
    bot.settings = {**bot.settings, "max_order_value": 500}
    db.add(Signal(symbol=SYMBOL, timestamp=datetime(2023, 1, 1), bot_name=bot.name, name="TRADE_TRIGGER", action="buy"))
    db.commit()

    def counts():
        db.expire_all()
        return (db.query(Signal).filter(Signal.bot_name == bot.name).count(),
                db.query(Position).filter(Position.bot_name == bot.name, Position.mode == "backtest").count())

    # Layout-only save: results survive (TestClient runs background tasks before returning)
    r = client.put(f"/api/bots/{bot.id}", json={"settings": {"ui_layout": {"nodes": [1]}, "live_allocation_pct": 50}}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert counts() == (1, 1)

    # Strategy change: stale signals + backtest trades are flushed
    r = client.put(f"/api/bots/{bot.id}", json={"settings": {"backtest_capital": 2000}}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert counts() == (0, 0)


def test_csv_export_streams_from_its_own_session(db, client, running_bot):
    running_bot(positions=[("live", SYMBOL, 0.5)])
    r = client.get("/api/trades/export", params={"mode": "live"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    rows = r.text.strip().splitlines()
    assert rows[0].startswith("ID,Timestamp,Bot Name")
    assert len(rows) == 2 and f",{SYMBOL},BUY,MARKET," in rows[1]


# ── 1.8 wallet swap ─────────────────────────────────────────────────────────

class _SwapExchange(ExchangeMock):
    def __init__(self, last=100.0, **kw):
        super().__init__(**kw)
        self.last = last

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": self.last}


def test_swap_rejects_malformed_body(db, client, running_bot):
    running_bot()
    bad = [{"from_asset": "usdt", "to_asset": "BTC", "amount": 1},
           {"from_asset": "USDT", "to_asset": "BTC", "amount": 0},
           {"from_asset": "USDT", "to_asset": "BTC", "amount": -5},
           {"from_asset": "USDT", "to_asset": "BTC"},
           {"from_asset": "USDT", "to_asset": "BTC", "amount": 1, "amount_type": "sideways"}]
    for body in bad:
        r = client.post(f"/api/keys/{KEY_NAME}/swap", json=body, headers=HEADERS)
        assert r.status_code == 422, (body, r.text)


def test_swap_caps_notional_and_is_idempotent(db, client, running_bot, monkeypatch):
    from backend.routers import keys as keys_router
    running_bot()
    ex = _SwapExchange(last=100.0)
    monkeypatch.setattr(keys_router, "get_authenticated_exchange", lambda key, **kw: ex)
    monkeypatch.setattr(keys_router.time, "sleep", lambda s: None)

    # 60 BTC × 100 = 6000 USDT > cap
    r = client.post(f"/api/keys/{KEY_NAME}/swap", json={"from_asset": "USDT", "to_asset": "BTC", "amount": 6000}, headers=HEADERS)
    assert r.status_code == 400 and "safety cap" in r.json()["detail"], r.text
    assert ex.created == []

    body = {"from_asset": "USDT", "to_asset": "BTC", "amount": 500, "idempotency_key": "tok-1"}
    r1 = client.post(f"/api/keys/{KEY_NAME}/swap", json=body, headers=HEADERS)
    assert r1.status_code == 200, r1.text
    assert [(o["side"], o["symbol"], o["amount"]) for o in ex.created] == [("buy", SYMBOL, 5.0)]
    r2 = client.post(f"/api/keys/{KEY_NAME}/swap", json=body, headers=HEADERS)
    assert r2.status_code == 200 and r2.json() == r1.json()
    assert len(ex.created) == 1  # the retry did not place a second order

    # Sell direction: FROM/TO exists as a market
    r = client.post(f"/api/keys/{KEY_NAME}/swap", json={"from_asset": "BTC", "to_asset": "USDT", "amount": 2, "amount_type": "from"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert ex.created[-1]["side"] == "sell" and ex.created[-1]["amount"] == 2.0


def test_summary_reports_the_mode_the_engine_would_book_in(db, client):
    db.add(ExchangeKey(name="sandbox-key", exchange=EXCHANGE, api_key="x", api_secret="y", passphrase="", is_sandbox=True))
    db.add(ExchangeKey(name="real-key", exchange=EXCHANGE, api_key="x", api_secret="y", passphrase="", is_sandbox=False))
    for name, api_exec, key in (("fwd", False, None), ("fwd-key-off", False, "real-key"), ("paper", True, "sandbox-key"),
                                ("live", True, "real-key"), ("missing-key", True, "gone")):
        s = _settings()
        s["api_execution"], s["api_key_name"] = api_exec, key
        db.add(BotConfig(name=name, is_active=False, is_sandbox=False, strategy="node_graph", settings=s))
    db.commit()

    modes = {b["name"]: b["execution_mode"] for b in client.get("/api/bots/summary", headers=HEADERS).json()}
    assert modes == {"fwd": "forward_test", "fwd-key-off": "forward_test", "paper": "paper", "live": "live",
                     "missing-key": "forward_test"}


def test_open_real_positions_cannot_be_deleted_only_closed_ones(db, client, running_bot):
    """Plan 3.3: dropping the record of a position that still exists on the
    exchange would orphan the coins. Open live/paper → 409; closed → deleted;
    open forward-test (nothing on the exchange) → deleted."""
    running_bot(positions=[("live", SYMBOL, 1.0), ("paper", SYMBOL, 1.0), ("forward_test", SYMBOL, 1.0)])
    live_id, paper_id, fwd_id = [
        r[0] for r in db.query(Position.id).order_by(Position.id).all()
    ]
    for pid in (live_id, paper_id):
        r = client.delete(f"/api/trades/positions/{pid}", headers=HEADERS)
        assert r.status_code == 409 and "Close it" in r.json()["detail"], r.text
    r = client.post("/api/trades/positions/bulk-delete", json=[live_id, fwd_id], headers=HEADERS)
    assert r.status_code == 409, r.text
    assert db.query(Position).count() == 3

    assert client.delete(f"/api/trades/positions/{fwd_id}", headers=HEADERS).status_code == 200
    db.query(Position).filter(Position.id == paper_id).update({"status": "closed"})
    db.commit()
    assert client.delete(f"/api/trades/positions/{paper_id}", headers=HEADERS).status_code == 200
    db.expire_all()
    assert [r[0] for r in db.query(Position.id).all()] == [live_id]


def test_symbols_endpoint_lists_active_spot_markets_and_degrades_to_unknown(client, monkeypatch):
    """Plan 3.4: the builder validates the whitelist against this. An
    unreachable exchange must answer known=false, never an empty allowlist."""
    from backend.core import exchange_registry as reg

    class Ex:
        def load_markets(self):
            return {"BTC/USDT": {"spot": True, "active": True}, "ETH/USDT": {"spot": True},
                    "OLD/USDT": {"spot": True, "active": False},
                    "BTC/USDT:USDT": {"spot": False, "swap": True, "type": "swap", "linear": True, "active": True},
                    "BTC/USD:BTC": {"spot": False, "swap": True, "type": "swap", "linear": False, "inverse": True}}
    monkeypatch.setattr(reg, "_markets_cache", {})
    monkeypatch.setattr(reg, "build_exchange", lambda exchange_id, **kw: Ex())
    r = client.get("/api/data/symbols/okx", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert {k: v for k, v in body.items() if k != "markets"} == {"exchange": "okx", "market_type": "spot", "symbols": ["BTC/USDT", "ETH/USDT"], "known": True}
    # Sprint D: `markets` describes each symbol's contract and cash unit
    assert body["markets"]["BTC/USDT"] == {"kind": "spot", "base": "BTC", "quote": "USDT", "settle": None, "contract_size": 1.0, "cash_currency": "USDT"}
    # Swap listing = linear AND inverse perpetuals, cached separately from spot
    r = client.get("/api/data/symbols/okx?market_type=swap", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["symbols"] == ["BTC/USD:BTC", "BTC/USDT:USDT"] and body["known"] is True
    assert body["markets"]["BTC/USD:BTC"]["kind"] == "inverse" and body["markets"]["BTC/USD:BTC"]["cash_currency"] == "BTC"
    assert body["markets"]["BTC/USDT:USDT"]["kind"] == "linear" and body["markets"]["BTC/USDT:USDT"]["settle"] == "USDT"
    assert client.get("/api/data/symbols/bitvavo?market_type=swap", headers=HEADERS).status_code == 400

    def boom(exchange_id, **kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(reg, "_markets_cache", {})
    monkeypatch.setattr(reg, "build_exchange", boom)
    r = client.get("/api/data/symbols/kraken", headers=HEADERS)
    assert r.status_code == 200 and r.json() == {"exchange": "kraken", "market_type": "spot", "symbols": [], "known": False, "markets": {}}
    assert client.get("/api/data/symbols/nope", headers=HEADERS).status_code == 400


def test_positions_and_orders_window_and_incremental_poll(db, client, running_bot):
    """Plan 4.3: `from`/`to` bound the payload server-side without ever hiding
    open exposure; `since_id`/`since` return only what changed since the last
    poll (new rows, rows closed since, and every open row)."""
    from datetime import datetime, timedelta
    bot = running_bot(positions=[("live", SYMBOL, 0.5)])
    open_id = db.query(Position.id).filter(Position.bot_name == bot.name).scalar()
    t0 = datetime(2020, 1, 1)  # well before the 2023 fixture candles
    old = Position(exchange=EXCHANGE, bot_name=bot.name, symbol=SYMBOL, mode="backtest", status="closed",
                   side="long", entry_price=1.0, amount=1.0, profit_abs=0.0, created_at=t0, closed_at=t0 + timedelta(hours=1))
    db.add(old)
    db.add(Order(exchange=EXCHANGE, bot_name=bot.name, mode="backtest", symbol=SYMBOL, side="buy", order_type="market",
                 price=1.0, amount=1.0, fee=0.0, status="filled", timestamp=t0, exchange_order_id="old"))
    db.commit()

    # Window after the old trade: the open live position is still returned
    r = client.get("/api/trades/positions", params={"from": "2022-01-01T00:00:00Z"}, headers=HEADERS)
    assert [p["id"] for p in r.json()] == [open_id]
    r = client.get("/api/trades/orders", params={"from": "2022-01-01T00:00:00Z"}, headers=HEADERS)
    seed_ts = db.query(Order.timestamp).filter(Order.exchange_order_id == "seed").scalar()
    assert [o["timestamp"] for o in r.json()] == [seed_ts.isoformat()]
    assert client.get("/api/trades/positions", params={"from": "yesterday"}, headers=HEADERS).status_code == 400

    # Incremental poll: nothing new → only the open row; after it closes with
    # closed_at >= since it is reported again
    max_id = max(p["id"] for p in client.get("/api/trades/positions", headers=HEADERS).json())
    r = client.get("/api/trades/positions", params={"since_id": max_id, "since": "2030-01-01T00:00:00Z"}, headers=HEADERS)
    assert [p["id"] for p in r.json()] == [open_id]
    db.query(Position).filter(Position.id == open_id).update({"status": "closed", "closed_at": datetime(2031, 1, 1)})
    db.commit()
    r = client.get("/api/trades/positions", params={"since_id": max_id, "since": "2030-01-01T00:00:00Z"}, headers=HEADERS)
    assert [(p["id"], p["status"]) for p in r.json()] == [(open_id, "closed")]
    r = client.get("/api/trades/positions", params={"since_id": max_id, "since": "2032-01-01T00:00:00Z"}, headers=HEADERS)
    assert r.json() == []
    max_order = max(o["id"] for o in client.get("/api/trades/orders", headers=HEADERS).json())
    assert client.get("/api/trades/orders", params={"since_id": max_order}, headers=HEADERS).json() == []


def test_chart_signals_and_bot_exchange_are_scoped_per_exchange(db, client):
    """The same pair + interval on another exchange is a different dataset:
    an OKX bot's signals must never land on a Binance chart. The chart scopes
    on the *candle* exchange — the key's exchange when orders are routed,
    else data_exchange."""
    from datetime import datetime, timezone
    from backend.models.signals import Signal

    db.add(ExchangeKey(name="okx-key", exchange="okx", api_key="x", api_secret="y", passphrase="", is_sandbox=True))
    for name, data_exchange, api_exec, key in (("bin-bot", "binance", False, None),
                                                ("okx-bot", "okx", False, None),
                                                ("routed-okx", "binance", True, "okx-key"),   # key wins
                                                ("key-off", "binance", False, "okx-key")):    # not routing → data_exchange
        s = _settings()
        s["data_exchange"], s["api_execution"], s["api_key_name"] = data_exchange, api_exec, key
        db.add(BotConfig(name=name, is_active=False, is_sandbox=False, strategy="node_graph", settings=s))
        db.add(Signal(symbol=SYMBOL, timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc), bot_name=name,
                      name="TRADE_TRIGGER", action="buy", value=1.0))
    db.commit()

    by_name = {b["name"]: b["exchange"] for b in client.get("/api/bots/", headers=HEADERS).json()}
    assert by_name == {"bin-bot": "binance", "okx-bot": "okx", "routed-okx": "okx", "key-off": "binance"}
    summary = {b["name"]: b["exchange"] for b in client.get("/api/bots/summary", headers=HEADERS).json()}
    assert summary == by_name

    def bots_on(exchange):
        r = client.get("/api/bots/signals", params={"symbol": SYMBOL, "timeframe": TF, "exchange": exchange}, headers=HEADERS)
        assert r.status_code == 200
        return sorted({s["bot_name"] for s in r.json()})

    assert bots_on("binance") == ["bin-bot", "key-off"]
    assert bots_on("OKX") == ["okx-bot", "routed-okx"]
    assert bots_on("kraken") == []


# ── audit 2026-09-24: sprint A/B router items ──────────────────────────────

def _wire_exchange(monkeypatch, mock):
    monkeypatch.setattr(trades_router, "get_authenticated_exchange", lambda key, **kw: mock)
    from backend.engine.bot_manager import BotManager, bot_manager
    real = BotManager._reconcile_order
    monkeypatch.setattr(bot_manager, "_reconcile_order",
                        lambda inst, order, sym, attempts=5, delay=1.0: real(bot_manager, inst, order, sym, attempts, 0))


def test_force_close_of_a_legacy_position_without_side_sells(db, client, running_bot, monkeypatch):
    """Item 1: rows from before shorts have `side=None`; they are longs and
    must be closed with a sell, never a buy."""
    running_bot(positions=[("live", SYMBOL, 0.5)])
    pos = db.query(Position).one()
    pos.side = None
    db.commit()
    mock = ExchangeMock(average=110.0)
    _wire_exchange(monkeypatch, mock)
    r = client.post(f"/api/trades/positions/{pos.id}/close", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert [(o["side"], o["amount"]) for o in mock.created] == [("sell", 0.5)]
    db.expire_all()
    pos = db.get(Position, pos.id)
    assert pos.status == "closed" and pos.profit_abs == pytest.approx(5.0)
    assert pos.profit_pct == pytest.approx(10.0)
    assert db.query(Order).filter(Order.position_id == pos.id).order_by(Order.id.desc()).first().side == "sell"


def test_manual_close_passes_leverage_where_the_exchange_wants_it_per_order(db, client, monkeypatch):
    """Item 20: kucoinfutures needs `leverage` in the close order params."""
    from tests.test_swap import SwapExchangeMock, SWAP, CONTRACT_SIZE
    candles = make_candles("kucoin", SWAP, TF, 30, seed=3, start_price=100.0)
    insert_candles(db, candles)
    db.add(ExchangeKey(name="kc", exchange="kucoin", api_key="x", api_secret="y", passphrase="p", is_sandbox=False, market_type="swap"))
    s = _settings(symbols=(SWAP,))
    s.update({"api_key_name": "kc", "data_exchange": "kucoin", "market_type": "swap", "leverage": 3, "margin_mode": "isolated"})
    db.add(BotConfig(name="kc-bot", is_active=True, is_sandbox=False, strategy="node_graph", settings=s))
    pos = Position(exchange="kucoin", bot_name="kc-bot", symbol=SWAP, mode="live", status="open", side="long",
                   entry_price=100.0, amount=0.5, created_at=candles[-2].timestamp, market_type="swap", leverage=3.0,
                   contracts=0.5 / CONTRACT_SIZE)
    db.add(pos)
    db.commit()
    mock = SwapExchangeMock(average=101.0)
    _wire_exchange(monkeypatch, mock)
    r = client.post(f"/api/trades/positions/{pos.id}/close", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert mock.created[0]["side"] == "sell"
    assert mock.created[0]["params"] == {"reduceOnly": True, "leverage": 3}


def test_key_save_and_delete_invalidate_the_leverage_cache(db, client, monkeypatch):
    """Item 20: a re-created key must confirm leverage/margin mode again."""
    from backend.engine import broker
    from backend.routers import keys as keys_router
    db.add(ExchangeKey(name="k1", exchange="binance", api_key="x", api_secret="y", passphrase="", is_sandbox=False))
    db.add(BotConfig(name="idle", is_active=False, is_sandbox=False, strategy="node_graph",
                     settings={**_settings(), "api_key_name": None, "api_execution": False}))
    db.commit()
    broker._leverage_applied.clear()
    broker._leverage_applied.update({("k1", "BTC/USDT:USDT", 3, "isolated"), ("other", "BTC/USDT:USDT", 3, "isolated")})
    r = client.delete("/api/keys/k1", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert broker._leverage_applied == {("other", "BTC/USDT:USDT", 3, "isolated")}

    broker._leverage_applied.add(("k1", "BTC/USDT:USDT", 3, "isolated"))
    monkeypatch.setattr(keys_router, "build_exchange", lambda *a, **kw: ExchangeMock())
    r = client.post("/api/keys", json={"name": "k1", "exchange": "binance", "api_key": "x", "api_secret": "y",
                                        "passphrase": "", "is_sandbox": False}, headers=HEADERS)
    assert r.status_code in (200, 201), r.text
    assert ("k1", "BTC/USDT:USDT", 3, "isolated") not in broker._leverage_applied
    broker._leverage_applied.clear()


def test_stats_capital_basis_is_the_sum_of_the_bots_in_view(db, client, running_bot):
    """Sprint B: the max-drawdown base of a multi-bot view is the sum of the
    bots' pools, not the largest one."""
    from datetime import datetime, timedelta
    a = running_bot(name="bot-a")
    b = running_bot(name="bot-b")
    a.settings = {**a.settings, "backtest_capital": 1000}
    b.settings = {**b.settings, "backtest_capital": 500}
    t0 = datetime(2024, 1, 1)
    for i, (name, pnl) in enumerate([("bot-a", -300.0), ("bot-b", -150.0)]):
        db.add(Position(exchange=EXCHANGE, bot_name=name, symbol=SYMBOL, mode="live", status="closed", side="long",
                        entry_price=100.0, amount=1.0, profit_abs=pnl, created_at=t0 + timedelta(hours=i),
                        closed_at=t0 + timedelta(hours=i + 1)))
    db.commit()
    r = client.get("/api/trades/stats", params={"mode": "live"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    # 450 lost on a 1500 base = 30% (would be 45% on max(1000, 500))
    assert r.json()["maxDDpct"] == pytest.approx(30.0)
    r = client.get("/api/trades/stats", params={"mode": "live", "bot_name": "bot-b"}, headers=HEADERS)
    assert r.json()["maxDDpct"] == pytest.approx(30.0)
