"""Router-level contract for stopping a bot that still holds real positions
(plan item 1.6). Runs through FastAPI's TestClient without the lifespan, so no
poller or engine thread is started; the exchange is the ``ExchangeMock`` from
the live-tick tests, wired into ``close_position_now`` via
``build_exchange_from_key``."""
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
    monkeypatch.setattr(trades_router, "build_exchange_from_key", lambda key: mock)
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

    def boom(key):
        raise RuntimeError("exchange down")
    monkeypatch.setattr(trades_router, "build_exchange_from_key", boom)
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
    db.add(Signal(symbol=SYMBOL, timestamp=datetime(2023, 1, 1), bot_name=bot.name, name="TRADE_TRIGGER", action="buy"))
    db.commit()

    def counts():
        db.expire_all()
        return (db.query(Signal).filter(Signal.bot_name == bot.name).count(),
                db.query(Position).filter(Position.bot_name == bot.name, Position.mode == "backtest").count())

    # Layout-only save: results survive (TestClient runs background tasks before returning)
    r = client.put(f"/api/bots/{bot.id}", json={"settings": {"ui_layout": {"nodes": [1]}, "max_order_value": 999}}, headers=HEADERS)
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
    monkeypatch.setattr(keys_router, "build_exchange_from_key", lambda key: ex)
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
