"""One live candle through ``BotManager._process_bots`` against a fake CCXT
exchange. The exchange seam is ``_get_ccxt_instance``; everything else (DB,
sizing, reconciliation, booking) is the real code path in ``live`` mode.

Tests marked ``xfail(strict=True)`` pin behaviour the audit plan changes in
phase 1 — they flip to passing (and must have the marker removed) when the fix
lands, so the fix and its test cannot drift apart.
"""
import asyncio

import pytest

from backend.engine.bot_manager import BotManager
from backend.models.bots import BotConfig
from backend.models.exchange_keys import ExchangeKey
from backend.models.orders import Order
from backend.models.positions import Position
from tests.conftest import insert_candles, make_candles

EXCHANGE, SYMBOL, TF = "binance", "BTC/USDT", "1h"
KEY_NAME = "mock-key"
N_CANDLES = 60


class ExchangeMock:
    """Minimal CCXT surface used by the live path."""

    def __init__(self, free_quote=10_000.0, status="closed", filled=None, average=None, fee=None,
                 cancel_raises=False, min_amount=0.0001, min_cost=5.0):
        self.free_quote = free_quote
        self.status = status
        self.filled = filled  # None → fill the requested amount
        self.average = average
        self.fee = fee
        self.cancel_raises = cancel_raises
        self.markets = {SYMBOL: {"symbol": SYMBOL, "limits": {"amount": {"min": min_amount}, "cost": {"min": min_cost}}}}
        self.created = []
        self.cancelled = []
        self._orders = {}

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.6f}"

    def fetch_balance(self):
        return {"USDT": {"free": self.free_quote, "used": 0.0, "total": self.free_quote},
                "free": {"USDT": self.free_quote}}

    def _create(self, side, symbol, amount):
        oid = f"mock-{len(self.created) + 1}"
        filled = amount if self.filled is None else self.filled
        order = {"id": oid, "symbol": symbol, "side": side, "amount": amount, "status": self.status,
                 "filled": filled, "average": self.average, "price": None, "fee": self.fee}
        self.created.append(order)
        self._orders[oid] = order
        return dict(order)

    def create_market_buy_order(self, symbol, amount):
        return self._create("buy", symbol, amount)

    def create_market_sell_order(self, symbol, amount):
        return self._create("sell", symbol, amount)

    def fetch_order(self, order_id, symbol=None):
        return dict(self._orders[order_id])

    def cancel_order(self, order_id, symbol=None):
        if self.cancel_raises:
            raise RuntimeError("order not found")
        self.cancelled.append(order_id)
        return {"id": order_id, "status": "canceled"}


def _settings(entry_always=True, max_positions=1, scope="per_pair", sl_pct=10, max_order_value=0, amount_pct=50):
    return {
        "symbols": [SYMBOL], "timeframe": TF, "data_exchange": EXCHANGE,
        "api_execution": True, "api_key_name": KEY_NAME,
        "backtest_on_start": False, "backtest_lookback": N_CANDLES, "backtest_capital": 1000,
        "max_positions": max_positions, "max_positions_scope": scope, "max_order_value": max_order_value,
        "live_allocation_pct": 100,
        "trade_settings": {
            "entry": {"order_type": "market", "amount_type": "percentage", "amount_value": amount_pct,
                      "fee": 0.1, "slippage": 0,
                      "stop_losses": [{"type": "percentage", "value": sl_pct, "close_amount_type": "percentage", "close_amount_value": 100}],
                      "take_profits": []},
            "exit": {"order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.1, "slippage": 0},
        },
        # `close > 0` is a trivially-true entry; `close < 0` never fires
        "nodes": {"entry": {"class": "condition", "left": "close", "operator": ">" if entry_always else "<", "right": 0}},
        "entry_node": "entry",
        "exit_node": None,
    }


@pytest.fixture
def live_bot(db):
    def _make(settings=None, last_low=None):
        candles = make_candles(EXCHANGE, SYMBOL, TF, N_CANDLES, seed=7, start_price=100.0)
        if last_low is not None:
            candles[-1].low = last_low
        insert_candles(db, candles)
        db.add(ExchangeKey(name=KEY_NAME, exchange=EXCHANGE, api_key="x", api_secret="y", passphrase="", is_sandbox=False))
        bot = BotConfig(name="live-bot", is_active=True, is_sandbox=False, strategy="node_graph",
                        settings=settings or _settings())
        db.add(bot)
        db.commit()
        return bot, candles
    return _make


@pytest.fixture
def run_tick(monkeypatch):
    """Run one candle-close for SYMBOL through a fresh manager wired to `mock`."""
    def _run(mock):
        bm = BotManager()
        monkeypatch.setattr(bm, "_get_ccxt_instance", lambda key_record: mock)
        # Reconciliation polls with a real 1 s sleep between attempts; not needed against a mock
        real = BotManager._reconcile_order
        monkeypatch.setattr(bm, "_reconcile_order",
                            lambda inst, order, sym, attempts=5, delay=1.0: real(bm, inst, order, sym, attempts, 0))
        asyncio.run(bm._process_bots(EXCHANGE, SYMBOL, TF))
        return bm
    return _run


def _open_live_position(db, candles, entry=100.0, amount=0.5):
    ts = candles[-2].timestamp
    pos = Position(exchange=EXCHANGE, bot_name="live-bot", symbol=SYMBOL, mode="live", status="open",
                   side="long", entry_price=entry, amount=amount, created_at=ts)
    db.add(pos)
    db.flush()
    db.add(Order(position_id=pos.id, exchange=EXCHANGE, bot_name="live-bot", mode="live", symbol=SYMBOL,
                 side="buy", order_type="market", price=entry, amount=amount, fee=0.0, status="filled",
                 timestamp=ts, exchange_order_id="seed"))
    db.commit()
    return pos


def _positions(db, status=None):
    q = db.query(Position).filter(Position.bot_name == "live-bot", Position.mode == "live")
    if status:
        q = q.filter(Position.status == status)
    return q.all()


# ── entries ────────────────────────────────────────────────────────────────

def test_buy_signal_opens_live_position_sized_from_wallet(db, live_bot, run_tick):
    bot, candles = live_bot()
    close = candles[-1].close
    mock = ExchangeMock(free_quote=10_000.0, average=close * 1.001)
    run_tick(mock)
    db.expire_all()

    assert len(mock.created) == 1
    buy = mock.created[0]
    assert buy["side"] == "buy"
    assert buy["amount"] == pytest.approx(0.5 * 10_000 / close, rel=1e-5)

    open_pos = _positions(db, "open")
    assert len(open_pos) == 1
    assert open_pos[0].entry_price == pytest.approx(close * 1.001)
    assert open_pos[0].amount == pytest.approx(buy["amount"])
    order = db.query(Order).filter(Order.bot_name == "live-bot", Order.mode == "live").one()
    assert order.status == "filled" and order.exchange_order_id == "mock-1"
    assert order.timestamp == candles[-1].timestamp


def test_no_signal_places_no_order(db, live_bot, run_tick):
    live_bot(_settings(entry_always=False))
    mock = ExchangeMock()
    run_tick(mock)
    assert mock.created == []
    assert _positions(db) == []


def test_replayed_candle_does_not_place_second_buy(db, live_bot, run_tick):
    live_bot(_settings(max_positions=2))
    mock = ExchangeMock()
    run_tick(mock)
    run_tick(mock)  # same latest candle again (restart / re-published event)
    assert len(mock.created) == 1
    assert len(_positions(db, "open")) == 1


def test_partial_fill_books_filled_amount(db, live_bot, run_tick):
    live_bot()
    mock = ExchangeMock(status="open", filled=0.02)
    run_tick(mock)
    db.expire_all()
    pos = _positions(db, "open")
    assert len(pos) == 1
    assert pos[0].amount == pytest.approx(0.02)
    assert mock.cancelled == []


def test_unfilled_buy_is_cancelled_and_not_booked(db, live_bot, run_tick):
    live_bot()
    mock = ExchangeMock(status="open", filled=0.0)
    run_tick(mock)
    assert mock.cancelled == ["mock-1"]
    assert _positions(db) == []
    order = db.query(Order).filter(Order.bot_name == "live-bot").one()
    assert order.status == "canceled"


def test_unknown_buy_state_books_position_at_requested_amount(db, live_bot, run_tick):
    live_bot()
    mock = ExchangeMock(status="open", filled=0.0, cancel_raises=True)
    run_tick(mock)
    db.expire_all()
    pos = _positions(db, "open")
    assert len(pos) == 1
    assert pos[0].amount == pytest.approx(mock.created[0]["amount"])


def test_base_currency_fee_reduces_held_amount(db, live_bot, run_tick):
    _, candles = live_bot()
    close = candles[-1].close
    mock = ExchangeMock(average=close, fee={"currency": "BTC", "cost": 0.001})
    run_tick(mock)
    db.expire_all()
    pos = _positions(db, "open")[0]
    requested = mock.created[0]["amount"]
    assert pos.amount == pytest.approx(requested - 0.001, abs=1e-6)
    order = db.query(Order).filter(Order.bot_name == "live-bot").one()
    assert order.fee == pytest.approx(0.001 * close)  # fee booked in quote


def test_min_notional_violation_skips_buy(db, live_bot, run_tick):
    live_bot(_settings(amount_pct=1))  # 1% of 100 USDT wallet = 1 USDT < 5 USDT min cost
    mock = ExchangeMock(free_quote=100.0, min_cost=5.0)
    run_tick(mock)
    assert mock.created == []
    assert _positions(db) == []


def test_max_order_value_currently_skips_entry_silently(db, live_bot, run_tick):
    # Pins today's behaviour (plan 1.3 changes this to clamp + warn)
    live_bot(_settings(max_order_value=250))
    mock = ExchangeMock(free_quote=10_000.0)  # 50% → 5000 USDT > 250 cap
    run_tick(mock)
    assert mock.created == []
    assert _positions(db) == []


# ── exits ──────────────────────────────────────────────────────────────────

def test_stop_loss_places_market_sell_and_closes_position(db, live_bot, run_tick):
    _, candles = live_bot(_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_live_position(db, candles, entry=100.0, amount=0.5)
    mock = ExchangeMock(average=90.0)
    run_tick(mock)
    db.expire_all()

    assert [o["side"] for o in mock.created] == ["sell"]
    assert mock.created[0]["amount"] == pytest.approx(0.5)
    closed = db.get(Position, pos.id)
    assert closed.status == "closed"
    assert closed.profit_abs == pytest.approx((90.0 - 100.0) * 0.5)


def test_unknown_sell_state_halts_bot_without_touching_position(db, live_bot, run_tick):
    _, candles = live_bot(_settings(entry_always=False), last_low=80.0)
    pos = _open_live_position(db, candles)
    mock = ExchangeMock(status="open", filled=0.0, cancel_raises=True)
    bm = run_tick(mock)
    db.expire_all()

    assert db.get(Position, pos.id).status == "open"
    assert db.get(Position, pos.id).amount == pytest.approx(0.5)
    bot = db.query(BotConfig).filter(BotConfig.name == "live-bot").one()
    assert bot.is_active is False
    assert "unknown" in (bot.settings.get("last_stop_reason") or "")
    assert bm.get_runtime("live-bot")["phase"] == "halted"
    unknown = db.query(Order).filter(Order.bot_name == "live-bot", Order.side == "sell").one()
    assert unknown.status == "unknown"


# ── phase-1 parity fixes (flip when implemented) ───────────────────────────

@pytest.mark.xfail(strict=True, reason="plan 1.1: a skipped entry must not skip SL/TP evaluation")
def test_skipped_entry_still_evaluates_stop_loss(db, live_bot, run_tick):
    # Entry fires but the allocation pool is exhausted (free=0, position already deployed) →
    # the entry is skipped; the 10% SL (low 80 < 90) must still sell.
    _, candles = live_bot(_settings(entry_always=True, max_positions=2), last_low=80.0)
    pos = _open_live_position(db, candles, entry=100.0, amount=0.5)
    mock = ExchangeMock(free_quote=0.0, average=90.0)
    run_tick(mock)
    db.expire_all()
    assert [o["side"] for o in mock.created] == ["sell"]
    assert db.get(Position, pos.id).status == "closed"


@pytest.mark.xfail(strict=True, reason="plan 1.2: live opens at most one position per pair")
def test_second_buy_on_open_symbol_is_skipped(db, live_bot, run_tick):
    _, candles = live_bot(_settings(entry_always=True, max_positions=2, scope="global"))
    _open_live_position(db, candles)
    mock = ExchangeMock(free_quote=10_000.0)
    run_tick(mock)
    assert mock.created == []
    assert len(_positions(db, "open")) == 1
