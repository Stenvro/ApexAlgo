"""Audit round 3 — live-path fixes (consensus_v2 M1, M2/R1, M3/R2, M4, M5,
M8/R4, M15, M21, M29).

Every test runs the real engine code path in ``live`` mode against the
``ExchangeMock``/``SwapExchangeMock`` fakes of the live-tick and swap suites;
nothing reaches the network.
"""
import asyncio
import os

import ccxt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.core.events import event_bus
from backend.engine import pnl
from backend.engine.bot_manager import BotManager, bot_manager
from backend.engine.candle_poller import CandlePoller
from backend.engine.sizing import position_spec
from backend.main import app
from backend.models.bot_logs import BotLog
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.orders import Order
from backend.models.positions import Position
from backend.routers import trades as trades_router
from tests import test_live_tick, test_swap
from tests.test_live_tick import EXCHANGE, SYMBOL, TF, ExchangeMock, _forward_settings, _open_live_position, _settings
from tests.test_swap import SWAP, SwapExchangeMock, _open_swap_position, _swap_settings

# Fixtures of the live-tick and swap suites (the alias keeps ruff's F811 quiet)
live_bot = test_live_tick.live_bot
swap_bot = test_swap.swap_bot
_fresh_leverage_cache = test_swap._fresh_leverage_cache

HEADERS = {"X-API-Key": os.environ["MASTER_API_KEY"]}


def _manager(monkeypatch, mock):
    """A manager wired to `mock`, reused across ticks so per-position state
    (triggered tiers, probe counter) carries over like in the real process."""
    bm = BotManager()
    monkeypatch.setattr(bm, "_get_ccxt_instance", lambda key_record, symbol=None: mock)
    real = BotManager._reconcile_order
    monkeypatch.setattr(bm, "_reconcile_order",
                        lambda inst, order, sym, attempts=5, delay=1.0: real(bm, inst, order, sym, attempts, 0))
    return bm


def _tick(bm, symbol=SWAP, candle_ts=None, replay=False):
    asyncio.run(bm._process_bots(EXCHANGE, symbol, TF, candle_ts, replay=replay))


def _logs(db, level, name="live-bot"):
    return [l.msg for l in db.query(BotLog).filter(BotLog.bot_name == name, BotLog.level == level).all()]


def _sells(db, pos_id, side="sell"):
    return db.query(Order).filter(Order.position_id == pos_id, Order.side == side).order_by(Order.id).all()


def _bot(db, name="live-bot"):
    db.expire_all()
    return db.query(BotConfig).filter(BotConfig.name == name).one()


# ── M1: a partial reduce-only fill keeps the tier armed ───────────────────

def test_partial_stop_fill_keeps_sl_armed_and_resends_the_remainder(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(status="open", filled=250, average=90.0)  # 250 contracts = 0.25 of the 0.5
    bm = _manager(monkeypatch, mock)
    _tick(bm)
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "open" and p.amount == pytest.approx(0.25)
    assert "sl_0" not in (p.triggered_exits or [])
    assert any("stays armed" in w for w in _logs(db, "WARN"))
    assert p.profit_abs == pytest.approx((90.0 - 100.0) * 0.25)

    # Next candle, low still through the stop: the remainder is re-sent and fills
    mock.status, mock.filled = "closed", None
    _tick(bm)
    db.expire_all()
    p = db.get(Position, pos.id)
    assert [o["amount"] for o in mock.created] == [pytest.approx(500), pytest.approx(250)]
    assert p.status == "closed" and "sl_0" in p.triggered_exits
    assert p.profit_abs == pytest.approx((90.0 - 100.0) * 0.5)


def test_partial_take_profit_fill_keeps_tp_armed(db, swap_bot, monkeypatch):
    s = _swap_settings(entry_always=False, sl_pct=50)
    s["trade_settings"]["entry"]["take_profits"] = [
        {"type": "percentage", "value": 10, "close_amount_type": "percentage", "close_amount_value": 50}]
    _, candles = swap_bot(s)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    db.query(Candle).filter(Candle.symbol == SWAP).order_by(Candle.timestamp.desc()).first().high = 120.0
    db.commit()
    mock = SwapExchangeMock(status="open", filled=100, average=110.0)  # asked 250 contracts, got 100
    bm = _manager(monkeypatch, mock)
    _tick(bm)
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "open" and p.amount == pytest.approx(0.4)
    assert "tp_0" not in (p.triggered_exits or [])

    mock.status, mock.filled = "closed", None
    _tick(bm)
    db.expire_all()
    p = db.get(Position, pos.id)
    # Second send asks the tier's full 50 % again (0.25), spends it, leaves 0.15 open
    assert [o["amount"] for o in mock.created] == [pytest.approx(250), pytest.approx(250)]
    assert p.status == "open" and p.amount == pytest.approx(0.15)
    assert "tp_0" in p.triggered_exits


# ── M2 (R1): network error on a close is never a liquidation ─────────────

def _timeout(symbol, order_type, side, amount, price=None, params=None):
    raise ccxt.RequestTimeout("read timeout")


def test_network_error_on_close_books_the_fill_found_in_the_trade_history(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(positions=[])  # flat because the close did fill
    mock.create_order = _timeout
    mock.my_trades = [
        {"id": "t1", "order": "o-9", "symbol": SWAP, "side": "sell", "amount": 300, "price": 90.0, "fee": {"cost": 0.03, "currency": "USDT"}},
        {"id": "t2", "order": "o-9", "symbol": SWAP, "side": "sell", "amount": 200, "price": 90.0, "fee": {"cost": 0.02, "currency": "USDT"}},
        {"id": "t0", "order": "o-1", "symbol": SWAP, "side": "buy", "amount": 500, "price": 100.0},
    ]
    bm = _manager(monkeypatch, mock)
    _tick(bm)
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "closed"
    assert "liquidation" not in p.triggered_exits and "sl_0" in p.triggered_exits
    assert p.profit_abs == pytest.approx((90.0 - 100.0) * 0.5 - 0.05)
    (sell,) = _sells(db, pos.id)
    assert sell.status == "filled" and sell.exchange_order_id == "o-9"
    assert sell.price == pytest.approx(90.0) and sell.amount == pytest.approx(0.5) and sell.fee == pytest.approx(0.05)
    assert _bot(db).is_active is True
    assert any("trade history" in w for w in _logs(db, "WARN"))


@pytest.mark.parametrize("history", ["empty", "raises"])
def test_network_error_on_close_without_a_fill_books_unknown_and_stops(db, swap_bot, monkeypatch, history):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(positions=[])  # even a flat exchange is no proof of a liquidation here
    mock.create_order = _timeout
    if history == "raises":
        def boom(*a, **k):
            raise ccxt.ExchangeNotAvailable("down")
        mock.fetch_my_trades = boom
    bm = _manager(monkeypatch, mock)
    _tick(bm)
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "open" and p.amount == pytest.approx(0.5)
    assert "liquidation" not in (p.triggered_exits or [])
    (unknown,) = _sells(db, pos.id)
    assert unknown.status == "unknown"
    bot = _bot(db)
    assert bot.is_active is False and "unknown" in bot.settings["last_stop_reason"]
    assert bm.get_runtime("live-bot")["phase"] == "halted"


def test_plain_exception_on_close_is_rejected_without_a_liquidation_probe(db, swap_bot, monkeypatch):
    """Only an exchange rejection (ccxt.ExchangeError) asks fetch_positions."""
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(positions=[])
    probes = []
    mock.fetch_positions = lambda symbols=None, params=None: probes.append(symbols) or []

    def boom(*a, **k):
        raise RuntimeError("something else")
    mock.create_order = boom
    _tick(_manager(monkeypatch, mock))
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "open" and probes == []
    assert [o.status for o in _sells(db, pos.id)] == ["rejected"]


# ── M3 (R2): paper/live liquidation detection ────────────────────────────

def test_live_liquidation_detected_from_candle_range_when_exchange_is_flat(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=90), last_low=40.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(positions=[])
    _tick(_manager(monkeypatch, mock))
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "closed" and "liquidation" in p.triggered_exits
    assert p.profit_abs == pytest.approx(-(100.0 * 0.5 / 3))
    assert mock.created == []  # nothing was sent for a position that is gone
    (liq,) = _sells(db, pos.id)
    spec = position_spec(SWAP, p.contract_kind, p.contract_size, EXCHANGE)
    # Booked at the modelled level incl. the exit-fee reserve (0.1 % in _settings), like the backtest
    assert liq.price == pytest.approx(spec.liquidation_price("long", 100.0, 3.0, fee=0.001))
    assert liq.exchange_order_id.startswith("liq_")


def test_live_level_hit_but_exchange_still_holds_it_runs_the_exits(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=10), last_low=40.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "long", "contracts": 500}], average=90.0)
    _tick(_manager(monkeypatch, mock))
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "closed" and "liquidation" not in p.triggered_exits and "sl_0" in p.triggered_exits
    assert [o["side"] for o in mock.created] == ["sell"]
    assert any("still holds" in w for w in _logs(db, "WARN"))


def test_live_periodic_probe_books_a_liquidation_the_candle_never_showed(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=90))
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(positions=[])
    probes = []
    mock.fetch_positions = lambda symbols=None, params=None: probes.append(list(symbols or [])) or []
    bm = _manager(monkeypatch, mock)
    for _ in range(5):
        _tick(bm)
    db.expire_all()
    assert db.get(Position, pos.id).status == "open" and probes == []
    _tick(bm)  # sixth processed candle
    db.expire_all()
    p = db.get(Position, pos.id)
    assert probes == [[SWAP]]
    assert p.status == "closed" and "liquidation" in p.triggered_exits
    assert p.profit_abs == pytest.approx(-(100.0 * 0.5 / 3))
    assert mock.created == []


def test_live_periodic_probe_is_one_fetch_per_symbol_and_keeps_held_positions(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=90, max_positions=2))
    a = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    b = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    held = [{"symbol": SWAP, "side": "long", "contracts": 1000}]
    mock = SwapExchangeMock(positions=held)
    probes = []
    mock.fetch_positions = lambda symbols=None, params=None: probes.append(1) or list(held)
    bm = _manager(monkeypatch, mock)
    for _ in range(6):
        _tick(bm)
    db.expire_all()
    assert len(probes) == 1
    assert db.get(Position, a.id).status == "open" and db.get(Position, b.id).status == "open"


def _capital_loss_settings():
    s = _swap_settings(entry_always=False, sl_pct=90)
    s["max_capital_loss"] = 10
    s["backtest_capital"] = 100
    return s


def _crash_last_close(db, close=60.0):
    db.query(Candle).filter(Candle.symbol == SWAP).order_by(Candle.timestamp.desc()).first().close = close
    db.commit()


def test_close_all_books_liquidation_when_the_exchange_rejects_a_close_and_is_flat(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_capital_loss_settings())
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    _crash_last_close(db)  # open loss 20 on 100 → capital-loss stop → close_all
    mock = SwapExchangeMock(positions=[])

    def refuse(*a, **k):
        raise ccxt.InvalidOrder("ReduceOnly Order is rejected")
    mock.create_order = refuse
    _tick(_manager(monkeypatch, mock))
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "closed" and "liquidation" in p.triggered_exits
    bot = _bot(db)
    assert bot.is_active is False
    assert "positions closed" in bot.settings["last_stop_reason"]
    assert "could not be closed" not in bot.settings["last_stop_reason"]


def test_close_all_stop_reason_is_honest_when_a_close_fails(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_capital_loss_settings())
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    _crash_last_close(db)
    mock = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "long", "contracts": 500}])

    def refuse(*a, **k):
        raise ccxt.InvalidOrder("rate limit")
    mock.create_order = refuse
    _tick(_manager(monkeypatch, mock))
    db.expire_all()
    assert db.get(Position, pos.id).status == "open"
    bot = _bot(db)
    assert bot.is_active is False
    assert "1 position(s) could not be closed" in bot.settings["last_stop_reason"]
    assert "positions closed" not in bot.settings["last_stop_reason"]


def test_close_all_network_error_without_a_fill_books_unknown_and_counts_as_failed(db, swap_bot, monkeypatch):
    _, candles = swap_bot(_capital_loss_settings())
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    _crash_last_close(db)
    mock = SwapExchangeMock(positions=[])  # flat, but a timeout proves nothing
    mock.create_order = _timeout
    _tick(_manager(monkeypatch, mock))
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "open" and "liquidation" not in (p.triggered_exits or [])
    assert [o.status for o in _sells(db, pos.id)] == ["unknown"]
    assert "1 position(s) could not be closed" in _bot(db).settings["last_stop_reason"]


# ── M4: force close with a partial fill ───────────────────────────────────

def _wire_force_close(monkeypatch, mock):
    monkeypatch.setattr(trades_router, "get_authenticated_exchange", lambda key_record, **kw: mock)
    real = BotManager._reconcile_order
    monkeypatch.setattr(bot_manager, "_reconcile_order",
                        lambda inst, order, sym, attempts=5, delay=1.0: real(bot_manager, inst, order, sym, attempts, 0))


def test_force_close_partial_fill_keeps_the_remainder_open(db, live_bot, monkeypatch):
    _, candles = live_bot(_settings(entry_always=False))
    pos = _open_live_position(db, candles, entry=100.0, amount=0.5)
    mock = ExchangeMock(status="open", filled=0.2, average=110.0)
    _wire_force_close(monkeypatch, mock)

    result = trades_router.close_position_detailed(pos, db)
    assert result["partial"] is True
    assert result["closed"] == pytest.approx(0.2) and result["remaining"] == pytest.approx(0.3)
    assert "0.2 of 0.5" in result["message"] and "0.3 remains open" in result["message"]
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "open" and p.amount == pytest.approx(0.3)
    assert p.profit_abs == pytest.approx((110.0 - 100.0) * 0.2)
    (sell,) = _sells(db, pos.id)
    assert sell.status == "filled" and sell.amount == pytest.approx(0.2)
    assert mock.cancelled == ["mock-1"]  # the resting remainder of the order was cancelled

    # The float-returning API used by bot stop/delete refuses to call it closed
    with pytest.raises(HTTPException) as ei:
        trades_router.close_position_now(pos, db)
    assert ei.value.status_code == 409 and "remains open" in ei.value.detail
    db.expire_all()
    assert db.get(Position, pos.id).status == "open" and db.get(Position, pos.id).amount == pytest.approx(0.1)


def test_force_close_route_reports_partial_with_200(db, live_bot, monkeypatch):
    _, candles = live_bot(_settings(entry_always=False))
    pos = _open_live_position(db, candles, entry=100.0, amount=0.5)
    _wire_force_close(monkeypatch, ExchangeMock(status="open", filled=0.2, average=110.0))
    r = TestClient(app).post(f"/api/trades/positions/{pos.id}/close", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partial"] is True and body["closed"] == pytest.approx(0.2) and body["remaining"] == pytest.approx(0.3)

    _wire_force_close(monkeypatch, ExchangeMock(average=110.0))
    r = TestClient(app).post(f"/api/trades/positions/{pos.id}/close", headers=HEADERS)
    assert r.status_code == 200 and r.json()["partial"] is False
    db.expire_all()
    assert db.get(Position, pos.id).status == "closed"


# ── M5: poller public-instance cache per contract kind ───────────────────

def test_poller_caches_binance_linear_and_inverse_on_separate_instances():
    poller = CandlePoller()
    assert poller._cache_key("binance", "BTC/USDT:USDT") != poller._cache_key("binance", "BTC/USD:BTC")
    assert poller._cache_key("binance", "BTC/USDT:USDT") != poller._cache_key("binance", "BTC/USDT")
    linear, _ = poller._get_public_exchange("binance", "BTC/USDT:USDT")
    inverse, _ = poller._get_public_exchange("binance", "BTC/USD:BTC")
    spot, _ = poller._get_public_exchange("binance", "BTC/USDT")
    assert linear.id == "binance" and inverse.id == "binancecoinm" and spot.id == "binance"
    assert linear is not inverse and linear is not spot
    assert len(poller._exchange_cache) == 3
    # Same kind → shared instance (one lock per class, as before)
    assert poller._get_public_exchange("binance", "ETH/USDT:USDT")[0] is linear
    assert poller._get_public_exchange("binance", "ETH/USD:ETH")[0] is inverse


# ── M8 (R4): replayed candles place no paper/live entries ────────────────

def test_replayed_candle_blocks_live_entries_but_runs_exits(db, live_bot, monkeypatch):
    _, candles = live_bot(_settings(entry_always=True, max_positions=2), last_low=80.0)
    pos = _open_live_position(db, candles, entry=100.0, amount=0.5)
    mock = ExchangeMock(free_quote=10_000.0, average=90.0)
    bm = _manager(monkeypatch, mock)
    _tick(bm, SYMBOL, candles[-1].timestamp, replay=True)
    db.expire_all()
    assert [o["side"] for o in mock.created] == ["sell"]
    assert db.get(Position, pos.id).status == "closed"
    infos = [m for m in _logs(db, "INFO") if "replaying" in m]
    assert len(infos) == 1

    # Same replay batch: no second INFO line; the newest candle trades again
    _tick(bm, SYMBOL, candles[-1].timestamp, replay=True)
    assert len([m for m in _logs(db, "INFO") if "replaying" in m]) == 1
    _tick(bm, SYMBOL, candles[-1].timestamp, replay=False)
    assert [o["side"] for o in mock.created] == ["sell", "buy"]


def test_replayed_candle_keeps_forward_test_entries(db, live_bot, monkeypatch):
    live_bot(_forward_settings(entry_always=True))
    bm = _manager(monkeypatch, ExchangeMock())
    _tick(bm, SYMBOL, None, replay=True)
    assert db.query(Position).filter(Position.mode == "forward_test", Position.status == "open").count() == 1


def test_poller_publishes_the_replay_flag(db):
    poller = CandlePoller()
    ts = int(db.query(Candle).count() * 0) + 1_700_000_000_000

    async def run():
        q = event_bus.subscribe("CANDLE_CLOSED")
        await poller._save_and_notify(EXCHANGE, SYMBOL, TF, [ts, 1, 2, 0.5, 1.5, 10], replay=True)
        await poller._save_and_notify(EXCHANGE, SYMBOL, TF, [ts + 3_600_000, 1, 2, 0.5, 1.5, 10])
        return [q.get_nowait(), q.get_nowait()]
    first, second = asyncio.run(run())
    assert first["replay"] is True and second["replay"] is False
    assert first["symbol"] == SYMBOL and second["timeframe"] == TF


# ── M15 / M29: bot stop closes off the event loop, brief carries the contract fields ──

def test_stop_closes_positions_in_a_thread_and_brief_has_contract_fields(db, live_bot):
    from backend.routers.bots import _position_brief, _stop
    _, candles = live_bot(_forward_settings(entry_always=False))
    pos = Position(exchange=EXCHANGE, bot_name="live-bot", symbol=SYMBOL, mode="forward_test", status="open",
                   side="long", entry_price=100.0, amount=1.0, created_at=candles[-2].timestamp)
    db.add(pos)
    db.commit()
    brief = _position_brief(pos)
    assert {"side", "leverage", "contract_kind", "contract_size", "cash_currency"} <= set(brief)
    assert brief["side"] == "long" and brief["leverage"] == 1
    assert brief["contract_kind"] == "spot" and brief["cash_currency"] == "USDT"
    bot = _bot(db)
    result = asyncio.run(_stop(bot, db, close_positions=True))
    assert [c["id"] for c in result["closed_positions"]] == [pos.id]
    db.expire_all()
    assert db.get(Position, pos.id).status == "closed"
    assert _bot(db).is_active is False


def test_positions_route_returns_triggered_exits_and_funding_paid(db, live_bot):
    _, candles = live_bot(_settings(entry_always=False))
    pos = _open_live_position(db, candles)
    pos.triggered_exits = ["sl_0", "liquidation"]
    pos.funding_paid = -1.25
    db.commit()
    r = TestClient(app).get("/api/trades/positions", headers=HEADERS)
    assert r.status_code == 200, r.text
    (row,) = [x for x in r.json() if x["id"] == pos.id]
    assert row["triggered_exits"] == ["sl_0", "liquidation"] and row["funding_paid"] == pytest.approx(-1.25)


# ── M21: spot close refused for insufficient funds ───────────────────────

def _wallet_with_base(mock, free_btc):
    real = mock.fetch_balance
    mock.fetch_balance = lambda: {**real(), "BTC": {"free": free_btc, "used": 0.0, "total": free_btc}}


def test_spot_close_rejected_for_insufficient_funds_sells_what_the_wallet_holds(db, live_bot, monkeypatch):
    _, candles = live_bot(_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_live_position(db, candles, entry=100.0, amount=0.5)
    mock = ExchangeMock(average=90.0)
    _wallet_with_base(mock, 0.45)
    calls = []
    real_sell = mock.create_market_sell_order

    def sell(symbol, amount):
        calls.append(float(amount))
        if len(calls) == 1:
            raise ccxt.InsufficientFunds("insufficient balance")
        return real_sell(symbol, amount)
    mock.create_market_sell_order = sell
    _tick(_manager(monkeypatch, mock), SYMBOL)
    db.expire_all()
    p = db.get(Position, pos.id)
    assert calls == [pytest.approx(0.5), pytest.approx(0.45)]
    assert p.status == "closed" and "sl_0" in p.triggered_exits
    assert p.profit_abs == pytest.approx((90.0 - 100.0) * 0.45)
    (sell_order,) = [o for o in _sells(db, pos.id) if o.status == "filled"]
    assert sell_order.amount == pytest.approx(0.45)
    assert any("selling what is held" in w for w in _logs(db, "WARN"))


def test_spot_close_rejected_with_dust_wallet_closes_administratively(db, live_bot, monkeypatch):
    _, candles = live_bot(_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_live_position(db, candles, entry=100.0, amount=0.5)
    mock = ExchangeMock(average=90.0, min_cost=5.0)
    _wallet_with_base(mock, 0.00001)  # ≈ 0.001 USDT, below the 5 USDT minimum

    def refuse(symbol, amount):
        raise ccxt.InsufficientFunds("insufficient balance")
    mock.create_market_sell_order = refuse
    _tick(_manager(monkeypatch, mock), SYMBOL)
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "closed" and mock.created == []
    assert [o.status for o in _sells(db, pos.id)] == ["rejected"]
    assert any("closed administratively" in w for w in _logs(db, "WARN"))


def test_swap_close_rejected_for_insufficient_funds_is_not_retried_from_the_wallet(db, swap_bot, monkeypatch):
    """The spot fallback never runs on a perpetual (nothing to sell from a wallet)."""
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0)
    mock = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "long", "contracts": 500}])
    balances = []
    real_bal = mock.fetch_balance
    mock.fetch_balance = lambda: balances.append(1) or real_bal()

    def refuse(*a, **k):
        raise ccxt.InsufficientFunds("insufficient margin")
    mock.create_order = refuse
    _tick(_manager(monkeypatch, mock))
    db.expire_all()
    assert db.get(Position, pos.id).status == "open" and balances == []
    assert [o.status for o in _sells(db, pos.id)] == ["rejected"]


def test_close_epsilon_is_the_partial_fill_tolerance():
    assert pnl.close_epsilon(0.5) == pytest.approx(5e-7)


def test_forward_cross_breach_adds_the_just_opened_cost_back(db):
    """M13 mirror: the layer opened at this close is excluded from the cross
    check and its margin + entry fee (already out of `forward_pool`) is added
    back to the cash — the account is tested as it stood before the entry."""
    from types import SimpleNamespace
    from backend.engine import live_cycle
    from backend.models.positions import Position

    older = Position(bot_name="x", symbol="BTC/USDT:USDT", exchange="okx", mode="forward_test", side="long",
                     amount=1.0, entry_price=100.0, leverage=10, status="open", market_type="swap",
                     cash_currency="USDT", contract_kind="linear", contract_size=1.0)
    bot = SimpleNamespace(name="x", settings={})
    # pool after the new entry: 0.5 free; the older 10x long at a −9% low has lost 9 of its 10 margin
    engine = SimpleNamespace(_forward_pool=lambda db, bot, ccy: 0.5)
    args = (db, engine, bot, [older], "okx", "1h", "BTC/USDT:USDT", 100.0, 91.0, None, {})
    assert live_cycle._forward_cross_breached(*args) is None               # equity 0.5 + 10 − 9 = 1.5 > maint ≈ 0.46
    args = (db, engine, bot, [older], "okx", "1h", "BTC/USDT:USDT", 100.0, 89.9, None, {})   # −10.1%: equity 0.4 ≤ maint → breach
    assert live_cycle._forward_cross_breached(*args) is not None
    # the just-opened layer's cost (say 5.0 margin + fee) added back keeps the account alive
    assert live_cycle._forward_cross_breached(*args, extra_cash=5.0) is None
