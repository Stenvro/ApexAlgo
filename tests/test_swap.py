"""Phase 2 — perpetual swaps (long-only) with leverage.

Live/paper: a swap bot sends `create_order` in contracts (reduce-only on
closes), confirms leverage first and reconciles against `fetch_positions`.
Backtest/forward: entries lock margin (notional / leverage) plus fee on the
notional, PnL is on the full notional, and a candle low through the
liquidation price closes the position at −margin. Spot bots do not touch any
of these branches (goldens pin that).
"""
import asyncio

import pytest

from backend.engine import broker
from backend.engine.backtest import liquidation_price
from backend.engine.bot_manager import BotManager
from backend.engine.settings_validator import validate_bot_settings
from backend.engine.sizing import _config_fingerprint
from backend.models.bots import BotConfig
from backend.models.exchange_keys import ExchangeKey
from backend.models.orders import Order
from backend.models.positions import Position
from tests.conftest import insert_candles, make_candles
from tests.test_live_tick import ExchangeMock, _settings, EXCHANGE, TF, KEY_NAME, N_CANDLES

SWAP = "BTC/USDT:USDT"
CONTRACT_SIZE = 0.001


class SwapExchangeMock(ExchangeMock):
    """ExchangeMock with a linear perpetual market, leverage calls and
    positions. `create_order` records its params (reduceOnly/leverage)."""

    def __init__(self, positions=None, leverage_raises=None, **kw):
        super().__init__(**kw)
        self.markets[SWAP] = {"symbol": SWAP, "type": "swap", "swap": True, "linear": True, "settle": "USDT",
                              "contractSize": CONTRACT_SIZE, "precision": {"amount": 1},
                              "limits": {"amount": {"min": 1}, "cost": {"min": 5.0}}}
        self.has = {"setLeverage": True, "setMarginMode": True, "fetchPositions": True}
        self.leverage_calls = []
        self.margin_calls = []
        self.positions = positions or []
        self.leverage_raises = leverage_raises

    def set_leverage(self, leverage, symbol=None, params=None):
        if self.leverage_raises:
            raise self.leverage_raises
        self.leverage_calls.append((leverage, symbol, dict(params or {})))
        return {}

    def set_margin_mode(self, mode, symbol=None, params=None):
        self.margin_calls.append((mode, symbol, dict(params or {})))
        return {}

    def fetch_positions(self, symbols=None, params=None):
        return list(self.positions)

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = self._create(side, symbol, amount)
        self.created[-1]["params"] = dict(params or {})
        return order


def _swap_settings(leverage=3, **kw):
    s = _settings(symbols=(SWAP,), **kw)
    s["market_type"] = "swap"
    s["leverage"] = leverage
    s["margin_mode"] = "isolated"
    return s


@pytest.fixture(autouse=True)
def _fresh_leverage_cache():
    broker._leverage_applied.clear()
    yield
    broker._leverage_applied.clear()


@pytest.fixture
def swap_bot(db):
    def _make(settings=None, key_market_type="swap", last_low=None):
        candles = make_candles(EXCHANGE, SWAP, TF, N_CANDLES, seed=7, start_price=100.0)
        if last_low is not None:
            candles[-1].low = last_low
        insert_candles(db, candles)
        db.add(ExchangeKey(name=KEY_NAME, exchange=EXCHANGE, api_key="x", api_secret="y", passphrase="",
                           is_sandbox=False, market_type=key_market_type))
        bot = BotConfig(name="live-bot", is_active=True, is_sandbox=False, strategy="node_graph",
                        settings=settings or _swap_settings())
        db.add(bot)
        db.commit()
        return bot, candles
    return _make


@pytest.fixture
def run_tick(monkeypatch):
    def _run(mock):
        bm = BotManager()
        monkeypatch.setattr(bm, "_get_ccxt_instance", lambda key_record: mock)
        real = BotManager._reconcile_order
        monkeypatch.setattr(bm, "_reconcile_order",
                            lambda inst, order, sym, attempts=5, delay=1.0: real(bm, inst, order, sym, attempts, 0))
        asyncio.run(bm._process_bots(EXCHANGE, SWAP, TF))
        return bm
    return _run


def _open_swap_position(db, candles, entry=100.0, amount=0.5, leverage=3.0):
    ts = candles[-2].timestamp
    pos = Position(exchange=EXCHANGE, bot_name="live-bot", symbol=SWAP, mode="live", status="open", side="long",
                   entry_price=entry, amount=amount, created_at=ts, market_type="swap", leverage=leverage,
                   contracts=amount / CONTRACT_SIZE)
    db.add(pos)
    db.flush()
    db.add(Order(position_id=pos.id, exchange=EXCHANGE, bot_name="live-bot", mode="live", symbol=SWAP, side="buy",
                 order_type="market", price=entry, amount=amount, fee=0.0, status="filled", timestamp=ts,
                 exchange_order_id="seed", market_type="swap"))
    db.commit()
    return pos


def _positions(db, mode="live", status=None):
    q = db.query(Position).filter(Position.bot_name == "live-bot", Position.mode == mode)
    if status:
        q = q.filter(Position.status == status)
    return q.all()


# ── live entries / exits ───────────────────────────────────────────────────

def test_swap_entry_sets_leverage_and_buys_contracts(db, swap_bot, run_tick):
    _, candles = swap_bot()
    close = candles[-1].close
    mock = SwapExchangeMock(free_quote=10_000.0, average=close)
    run_tick(mock)
    db.expire_all()

    # leverage/margin mode confirmed before the first order
    assert mock.margin_calls == [("isolated", SWAP, {"leverage": 3})]
    assert mock.leverage_calls == [(3, SWAP, {"marginMode": "isolated"})]

    assert len(mock.created) == 1
    buy = mock.created[0]
    assert buy["side"] == "buy" and buy["symbol"] == SWAP
    assert "reduceOnly" not in buy["params"] and "leverage" not in buy["params"]  # binance: leverage is per symbol
    # 50% of the wallet as margin, times 3x leverage, expressed in contracts
    base_amount = 0.5 * 10_000 * 3 / close
    assert buy["amount"] == pytest.approx(base_amount / CONTRACT_SIZE, rel=1e-5)

    pos = _positions(db, status="open")
    assert len(pos) == 1
    assert pos[0].market_type == "swap" and pos[0].leverage == 3.0
    assert pos[0].amount == pytest.approx(base_amount, rel=1e-5)
    assert pos[0].contracts == pytest.approx(buy["amount"], rel=1e-5)
    order = db.query(Order).filter(Order.bot_name == "live-bot", Order.mode == "live").one()
    assert order.market_type == "swap" and not order.reduce_only and order.amount == pytest.approx(base_amount, rel=1e-5)


def test_swap_entry_is_skipped_when_leverage_cannot_be_set(db, swap_bot, run_tick):
    swap_bot()
    mock = SwapExchangeMock(leverage_raises=RuntimeError("margin insufficient"))
    run_tick(mock)
    db.expire_all()
    assert mock.created == []
    assert _positions(db, status="open") == []
    rejected = db.query(Order).filter(Order.bot_name == "live-bot").all()
    assert [o.status for o in rejected] == ["rejected"]


def test_swap_stop_loss_sells_reduce_only_in_contracts(db, swap_bot, run_tick):
    _, candles = swap_bot(_swap_settings(entry_always=False, sl_pct=10), last_low=80.0)
    pos = _open_swap_position(db, candles, entry=100.0, amount=0.5)
    mock = SwapExchangeMock(average=90.0)
    run_tick(mock)
    db.expire_all()

    assert len(mock.created) == 1
    sell = mock.created[0]
    assert sell["side"] == "sell" and sell["params"] == {"reduceOnly": True}
    assert sell["amount"] == pytest.approx(0.5 / CONTRACT_SIZE)
    assert mock.leverage_calls == []  # closing never touches leverage
    closed = db.get(Position, pos.id)
    assert closed.status == "closed"
    assert closed.profit_abs == pytest.approx((90.0 - 100.0) * 0.5)
    close_order = db.query(Order).filter(Order.position_id == pos.id, Order.side == "sell").one()
    assert close_order.reduce_only == 1 and close_order.market_type == "swap"
    assert close_order.amount == pytest.approx(0.5)


def test_swap_force_close_all_is_reduce_only(db, swap_bot, monkeypatch):
    bot, candles = swap_bot()
    pos = _open_swap_position(db, candles, amount=0.5)
    bm = BotManager()
    mock = SwapExchangeMock(average=101.0)
    monkeypatch.setattr(bm, "_get_ccxt_instance", lambda key_record: mock)
    real = BotManager._reconcile_order
    monkeypatch.setattr(bm, "_reconcile_order",
                        lambda inst, order, sym, attempts=5, delay=1.0: real(bm, inst, order, sym, attempts, 0))
    key = db.query(ExchangeKey).filter(ExchangeKey.name == KEY_NAME).one()
    bm._close_all_open_positions(bot, db, {KEY_NAME: key})
    db.expire_all()

    assert [(o["side"], o["params"], o["amount"]) for o in mock.created] == [("sell", {"reduceOnly": True}, pytest.approx(0.5 / CONTRACT_SIZE))]
    closed = db.get(Position, pos.id)
    assert closed.status == "closed" and closed.profit_abs == pytest.approx((101.0 - 100.0) * 0.5)
    close_order = db.query(Order).filter(Order.position_id == pos.id, Order.side == "sell").one()
    assert close_order.reduce_only == 1 and close_order.market_type == "swap" and close_order.amount == pytest.approx(0.5)


def test_spot_key_on_swap_bot_is_refused_by_validator(db, swap_bot):
    settings = _swap_settings(max_order_value=500)
    out = validate_bot_settings(dict(settings), exchange_id=EXCHANGE, key_market_type="spot")
    assert any("is a spot key but the bot trades swap" in e for e in out["errors"])
    out = validate_bot_settings(dict(settings), exchange_id=EXCHANGE, key_market_type="swap")
    assert out["errors"] == []


# ── reconciliation via fetch_positions ─────────────────────────────────────

def test_startup_reconciliation_uses_exchange_positions_for_swaps(db, swap_bot):
    bot, candles = swap_bot()
    _open_swap_position(db, candles, amount=0.5)
    bm = BotManager()

    ok = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "long", "contracts": 500, "contractSize": CONTRACT_SIZE}])
    assert bm._reconcile_positions_with_exchange(db, bot, ok, "live") == []

    short = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "long", "contracts": 200, "contractSize": CONTRACT_SIZE}])
    problems = bm._reconcile_positions_with_exchange(db, bot, short, "live")
    assert len(problems) == 1 and "0.5" in problems[0] and "0.2" in problems[0]

    flipped = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "short", "contracts": 500, "contractSize": CONTRACT_SIZE}])
    problems = bm._reconcile_positions_with_exchange(db, bot, flipped, "live")
    assert len(problems) == 1 and "short" in problems[0]

    assert bm._reconcile_positions_with_exchange(db, bot, SwapExchangeMock(), "paper") == []


# ── validator / fingerprint ────────────────────────────────────────────────

def test_validator_market_type_rules():
    s = _swap_settings(leverage=3, max_order_value=500)
    assert validate_bot_settings(dict(s), exchange_id="binance")["errors"] == []
    # spot pair on a swap bot, swap pair on a spot bot
    bad = _swap_settings(); bad["symbols"] = ["BTC/USDT"]
    assert any("BASE/QUOTE:SETTLE" in e for e in validate_bot_settings(bad, exchange_id="binance")["errors"])
    bad = _settings(symbols=(SWAP,))
    assert any("perpetual swap" in e for e in validate_bot_settings(bad, exchange_id="binance")["errors"])
    # leverage limits
    bad = _swap_settings(leverage=50)
    assert any("exceeds" in e for e in validate_bot_settings(bad, exchange_id="binance")["errors"])
    warned = validate_bot_settings(_swap_settings(leverage=5, max_order_value=500), exchange_id="binance")
    assert warned["errors"] == [] and any("liquidation" in w for w in warned["warnings"])
    bad = _settings(); bad["leverage"] = 2
    assert any("spot bot" in e for e in validate_bot_settings(bad, exchange_id="binance")["errors"])
    # exchange without swaps, bad margin mode
    assert any("no 'swap' market" in e for e in validate_bot_settings(_swap_settings(), exchange_id="bitvavo")["errors"])
    bad = _swap_settings(); bad["margin_mode"] = "hedge"
    assert any("margin_mode" in e for e in validate_bot_settings(bad, exchange_id="binance")["errors"])


def test_fingerprint_ignores_phase2_defaults_but_counts_leverage():
    spot = _settings()
    explicit = dict(spot, market_type="spot", leverage=1, margin_mode="isolated")
    assert _config_fingerprint(explicit) == _config_fingerprint(spot)
    assert _config_fingerprint(dict(explicit, leverage=2)) != _config_fingerprint(spot)
    assert _config_fingerprint(dict(explicit, market_type="swap")) != _config_fingerprint(spot)


# ── backtest economics ─────────────────────────────────────────────────────

def _bt_settings(leverage, market_type="swap", capital=1000, sl=3, tp=3, amount_pct=40):
    sym = SWAP if market_type == "swap" else "BTC/USDT"
    s = _settings(symbols=(sym,), amount_pct=amount_pct, sl_pct=sl, max_positions=1)
    s.update({"api_execution": False, "api_key_name": None, "backtest_on_start": True,
              "backtest_lookback": 300, "backtest_capital": capital,
              "market_type": market_type, "leverage": leverage, "margin_mode": "isolated"})
    s["trade_settings"]["entry"]["take_profits"] = [{"type": "percentage", "value": tp, "close_amount_type": "percentage", "close_amount_value": 100}]
    return s


def _run_backtest(db, settings, seed=11, symbol=SWAP, name="bt-bot"):
    insert_candles(db, make_candles(EXCHANGE, symbol, TF, 400, seed=seed, start_price=100.0))
    bot = BotConfig(name=name, is_active=True, is_sandbox=True, strategy="node_graph", settings=settings)
    db.add(bot)
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    bot = db.query(BotConfig).filter(BotConfig.name == name).one()
    positions = db.query(Position).filter(Position.bot_name == name, Position.mode == "backtest").order_by(Position.id).all()
    return bot, positions


def test_backtest_leverage_scales_notional_and_locks_margin(db):
    bot, positions = _run_backtest(db, _bt_settings(leverage=3))
    assert positions, "the always-true entry must trade"
    first = positions[0]
    assert first.market_type == "swap" and first.leverage == 3.0
    # 40% of 1000 as margin at 3x → notional 1200, minus the fee on the notional
    fee = 0.001
    max_affordable = 1000 / (first.entry_price * (1 / 3 + fee))
    assert first.amount <= max_affordable + 1e-9
    assert first.amount * first.entry_price == pytest.approx(0.4 * 1000 * 3, rel=1e-6)
    summary = bot.settings["last_backtest_summary"]
    assert summary["market_type"] == "swap" and summary["leverage"] == 3 and summary["funding"] == "ignored"
    assert "liquidations" in summary
    orders = db.query(Order).filter(Order.bot_name == "bt-bot").all()
    assert orders and all(o.market_type == "swap" for o in orders)
    assert any(o.reduce_only for o in orders if o.side == "sell")


def test_backtest_1x_swap_matches_spot_pnl_per_trade(db):
    """With leverage 1 the swap maths must collapse to the spot maths."""
    _, swap_pos = _run_backtest(db, _bt_settings(leverage=1), symbol=SWAP, name="swap-bot")
    _, spot_pos = _run_backtest(db, _bt_settings(leverage=1, market_type="spot"), symbol="BTC/USDT", name="spot-bot")
    assert len(swap_pos) == len(spot_pos) > 0
    for a, b in zip(swap_pos, spot_pos):
        assert a.entry_price == pytest.approx(b.entry_price)
        assert a.amount == pytest.approx(b.amount)
        assert a.profit_abs == pytest.approx(b.profit_abs)


def test_backtest_liquidation_closes_at_minus_margin(db):
    """A wide stop and 10x leverage: the first deep candle liquidates before
    the stop fires, losing exactly the margin and paying no exit fee."""
    lev = 10
    bot, positions = _run_backtest(db, _bt_settings(leverage=lev, sl=50, tp=500, amount_pct=20), seed=5)
    summary = bot.settings["last_backtest_summary"]
    liq = [p for p in positions if "liquidation" in (p.triggered_exits or [])]
    assert summary["liquidations"] >= 1 and liq
    p = liq[0]
    margin = p.entry_price * p.amount / lev
    assert p.profit_abs == pytest.approx(-margin, rel=1e-6)
    sell = db.query(Order).filter(Order.position_id == p.id, Order.side == "sell").one()
    assert sell.price == pytest.approx(liquidation_price(p.entry_price, lev), rel=1e-9)
    assert sell.fee == 0


def test_liquidation_price_formula():
    assert liquidation_price(100.0, 1) == pytest.approx(0.5)  # 1x: only a total wipe-out
    assert liquidation_price(100.0, 10) == pytest.approx(100 * (1 - 0.995 / 10))
    assert liquidation_price(100.0, 2) < 100.0


# ── forward test ───────────────────────────────────────────────────────────

def test_forward_swap_entry_sizes_margin_from_pool(db, swap_bot, run_tick):
    s = _swap_settings(leverage=4, amount_pct=50)
    s["api_execution"] = False
    s["api_key_name"] = None
    s["trade_settings"]["entry"]["slippage"] = 0.5
    _, candles = swap_bot(s)
    close = candles[-1].close
    run_tick(SwapExchangeMock())
    db.expire_all()
    pos = _positions(db, mode="forward_test", status="open")
    assert len(pos) == 1
    entry_price = close * 1.005
    assert pos[0].entry_price == pytest.approx(entry_price)
    assert pos[0].market_type == "swap" and pos[0].leverage == 4.0
    # 50% of the 1000 pool as margin, x4 notional; must fit margin + fee
    assert pos[0].amount == pytest.approx(0.5 * 1000 * 4 / close, rel=1e-6)
    assert pos[0].amount * entry_price * (1 / 4 + 0.001) <= 1000 + 1e-6
