"""Phase 3 — shorts on perpetual swaps.

A `short` action opens a short layer (Order side "sell", not reduce-only), a
`cover` action flattens it (Order side "buy", reduce-only). Exit rules are
the mirror of the long ones (SL above the entry hit by the high, TP below hit
by the low, trailing anchored to the lowest price), PnL is `(entry − exit)`
and liquidation sits above the entry. Long/spot paths are untouched: the
goldens pin that.
"""
import pytest

from backend.engine import pnl
from backend.engine.bot_manager import BotManager
from backend.engine.settings_validator import validate_bot_settings
from backend.engine.sizing import _config_fingerprint
from backend.models.bots import BotConfig
from backend.models.orders import Order
from backend.models.positions import Position
from tests.conftest import insert_candles, make_candles
from tests.test_live_tick import EXCHANGE, TF
from tests import test_swap
from tests.test_swap import SWAP, CONTRACT_SIZE, SwapExchangeMock, _swap_settings, _bt_settings, _positions

# Re-export the swap fixtures under their own names so pytest finds them here
swap_bot = test_swap.swap_bot
run_tick = test_swap.run_tick
_fresh_leverage_cache = test_swap._fresh_leverage_cache


# ── helpers ────────────────────────────────────────────────────────────────

def _spos(entry=100.0, amount=1.0, pid=1, lowest=None, triggered=None):
    return Position(id=pid, exchange="binance", bot_name="t", symbol=SWAP, mode="backtest",
                    status="open", side="short", entry_price=entry, amount=amount,
                    highest_price=lowest, triggered_exits=triggered, market_type="swap", leverage=2)


def _lpos(entry=100.0, amount=1.0, pid=1, highest=None, triggered=None):
    return Position(id=pid, exchange="binance", bot_name="t", symbol="BTC/USDT", mode="backtest",
                    status="open", side="long", entry_price=entry, amount=amount,
                    highest_price=highest, triggered_exits=triggered)


def _rule(kind, value, close_pct=100):
    return {"type": kind, "value": value, "close_amount_type": "percentage", "close_amount_value": close_pct}


def _exit_settings(stop_losses=(), take_profits=(), cover_amount=None, leg="entry"):
    ts = {"entry": {"stop_losses": [], "take_profits": []}, "exit": {}}
    ts[leg] = {"stop_losses": list(stop_losses), "take_profits": list(take_profits)}
    if cover_amount is not None:
        ts["cover"] = {"amount_type": "percentage", "amount_value": cover_amount}
    return {"trade_settings": ts}


def ids(events):
    return [e["id"] for e in events]


def _short_settings(short_always=True, cover=False, entry_always=False, leverage=3, **kw):
    """Swap settings with a `short` node (`close > 0` always fires) and an
    optional `cover` node; the long entry never fires unless asked."""
    s = _swap_settings(leverage=leverage, entry_always=entry_always, **kw)
    s["nodes"]["short"] = {"class": "condition", "left": "close", "operator": ">" if short_always else "<", "right": 0}
    s["short_node"] = "short"
    if cover:
        s["nodes"]["cover"] = {"class": "condition", "left": "close", "operator": ">", "right": 0}
        s["cover_node"] = "cover"
    else:
        s["cover_node"] = None
    return s


def _open_short_position(db, candles, entry=100.0, amount=0.5, leverage=3.0, mode="live"):
    ts = candles[-2].timestamp
    pos = Position(exchange=EXCHANGE, bot_name="live-bot", symbol=SWAP, mode=mode, status="open", side="short",
                   entry_price=entry, amount=amount, created_at=ts, market_type="swap", leverage=leverage,
                   contracts=amount / CONTRACT_SIZE)
    db.add(pos)
    db.flush()
    db.add(Order(position_id=pos.id, exchange=EXCHANGE, bot_name="live-bot", mode=mode, symbol=SWAP, side="sell",
                 order_type="market", price=entry, amount=amount, fee=0.0, status="filled", timestamp=ts,
                 exchange_order_id="seed", market_type="swap", reduce_only=0))
    db.commit()
    return pos


# ── pnl helpers ────────────────────────────────────────────────────────────

def test_pnl_helpers_mirror_long_and_short():
    assert pnl.price_pnl("long", 100, 110, 2) == pytest.approx(20)
    assert pnl.price_pnl("short", 100, 110, 2) == pytest.approx(-20)
    assert pnl.price_pnl(None, 100, 110, 2) == pytest.approx(20)  # legacy rows are longs
    assert pnl.liquidation_price("short", 100.0, 10) == pytest.approx(100 * (1 + 0.995 / 10))
    assert pnl.liquidation_price("long", 100.0, 10) == pytest.approx(100 * (1 - 0.995 / 10))
    assert pnl.liquidation_price("short", 100.0, 1) is None
    assert pnl.liquidated("short", 110.0, 111.0, 100.0) and not pnl.liquidated("short", 110.0, 109.0, 100.0)
    assert pnl.liquidated("long", 90.0, 100.0, 89.0) and not pnl.liquidated("long", 90.0, 100.0, 91.0)
    assert pnl.open_order_side("short") == "sell" and pnl.close_order_side("short") == "buy"
    assert pnl.open_order_side("long") == "buy" and pnl.close_order_side("long") == "sell"
    ts = {"entry": {"a": 1}, "exit": {"b": 2}}
    assert pnl.entry_cfg(ts, "short") is ts["entry"] and pnl.exit_cfg(ts, "short") is ts["exit"]
    ts["short"], ts["cover"] = {"c": 3}, {"d": 4}
    assert pnl.entry_cfg(ts, "short") is ts["short"] and pnl.exit_cfg(ts, "short") is ts["cover"]
    assert pnl.entry_cfg(ts, "long") is ts["entry"] and pnl.exit_cfg(ts, "long") is ts["exit"]


# ── exits: mirrored rules ──────────────────────────────────────────────────

@pytest.fixture
def bm():
    return BotManager()


def _short(bm, pos, close, high, low, cover, settings, **kw):
    return bm._check_exits(pos, close, high, low, cover, settings, side="short", **kw)


def test_short_percentage_sl_hit_by_high_fills_at_trigger(bm):
    s = _exit_settings([_rule("percentage", 10)])
    ev = _short(bm, _spos(), 108, 111, 104, False, s, row_open=105)
    assert ids(ev) == ["sl_0"] and ev[0]["reason"] == "stop_loss"
    assert ev[0]["price"] == pytest.approx(110)
    # gap above the trigger fills at the (worse) open
    ev = _short(bm, _spos(pid=2), 115, 118, 112, False, s, row_open=113)
    assert ev[0]["price"] == pytest.approx(113)
    # the high staying below the trigger is no stop
    assert _short(bm, _spos(pid=3), 105, 109.9, 100, False, s) == []


def test_short_percentage_tp_hit_by_low_fills_at_target(bm):
    s = _exit_settings(take_profits=[_rule("percentage", 5)])
    ev = _short(bm, _spos(), 96, 99, 94, False, s, row_open=98)
    assert ids(ev) == ["tp_0"] and ev[0]["reason"] == "take_profit"
    assert ev[0]["price"] == pytest.approx(95)
    # gap below the target fills at the (better) open
    ev = _short(bm, _spos(pid=2), 90, 93, 88, False, s, row_open=92)
    assert ev[0]["price"] == pytest.approx(92)


def test_short_trailing_sl_anchors_to_previous_lowest(bm):
    s = _exit_settings([_rule("trailing", 5)])
    pos = _spos(lowest=80.0)
    # trail = 80 * 1.05 = 84; a high of 85 triggers it, the new low is folded in afterwards
    ev = _short(bm, pos, 84, 85, 78, False, s, row_open=82)
    assert ids(ev) == ["sl_0"] and ev[0]["price"] == pytest.approx(84)
    assert pos.highest_price == pytest.approx(78)  # column carries the lowest for shorts
    # a candle that only makes a new low never triggers on its own range
    pos2 = _spos(pid=2, lowest=80.0)
    assert _short(bm, pos2, 78, 83, 76, False, s) == []
    assert pos2.highest_price == pytest.approx(76)


def test_short_trailing_tp_activates_below_entry_then_covers_on_bounce(bm):
    s = _exit_settings(take_profits=[_rule("trailing", 10)])
    # not activated: lowest 95 is above the 90 activation level
    assert _short(bm, _spos(lowest=95.0), 100, 106, 95, False, s) == []
    # activated at lowest 85 → target 85 * 1.10 = 93.5, hit by the high
    ev = _short(bm, _spos(pid=2, lowest=85.0), 93, 94, 88, False, s, row_open=90)
    assert ids(ev) == ["tp_0"] and ev[0]["price"] == pytest.approx(93.5)


def test_short_atr_exits_use_lowest_plus_atr(bm):
    s = _exit_settings([_rule("atr", 2)], [_rule("atr", 1)])
    # SL: 80 + 2*3 = 86 hit by the high → stop wins, TPs not evaluated
    ev = _short(bm, _spos(lowest=80.0), 85, 87, 82, False, s, current_atr=3.0, row_open=84)
    assert ids(ev) == ["sl_0"] and ev[0]["price"] == pytest.approx(86)
    # no ATR → ATR rules skipped
    assert _short(bm, _spos(pid=2, lowest=80.0), 85, 87, 82, False, s, current_atr=0.0) == []


def test_short_fixed_sl_and_tp(bm):
    s = _exit_settings([_rule("fixed", 120)], [_rule("fixed", 80)])
    assert ids(_short(bm, _spos(), 118, 121, 110, False, s)) == ["sl_0"]
    ev = _short(bm, _spos(pid=2), 82, 90, 79, False, s, row_open=85)
    assert ids(ev) == ["tp_0"] and ev[0]["price"] == pytest.approx(80)


def test_short_multi_tp_orders_lowest_first_and_remembers_triggered(bm):
    s = _exit_settings(take_profits=[_rule("percentage", 5, 50), _rule("percentage", 10, 50)])
    ev = _short(bm, _spos(), 88, 96, 87, False, s, row_open=95)
    assert ids(ev) == ["tp_1", "tp_0"]
    assert [e["price"] for e in ev] == [pytest.approx(90), pytest.approx(95)]
    assert ids(_short(bm, _spos(pid=2, triggered=["tp_0"]), 92, 96, 91, False, s)) == []


def test_short_strategy_cover_uses_cover_leg_and_yields_to_stops(bm):
    s = _exit_settings(cover_amount=40)
    ev = _short(bm, _spos(), 101, 102, 99, True, s)
    assert ids(ev) == ["strategy_cover"] and ev[0]["qty_pct"] == 40 and ev[0]["price"] == 101
    # a stop on the same candle wins over the cover signal
    s2 = _exit_settings([_rule("percentage", 1)], cover_amount=40)
    assert ids(_short(bm, _spos(pid=2), 101, 102, 99, True, s2)) == ["sl_0"]
    # no cover signal, no events
    assert _short(bm, _spos(pid=3), 101, 102, 99, False, s) == []


def test_short_leg_overrides_entry_leg(bm):
    s = _exit_settings([_rule("percentage", 2)], leg="short")
    s["trade_settings"]["entry"] = {"stop_losses": [_rule("percentage", 50)], "take_profits": []}
    assert ids(_short(bm, _spos(), 101, 103, 100, False, s)) == ["sl_0"]  # 2% short stop, not the 50% long one


def test_short_exit_is_mirror_of_long_exit(bm):
    """A short on a price path is the long on the mirrored path (p → 200 − p),
    fills mirrored too. Entry-relative percentage, ATR and fixed rules are
    additively symmetric around the 100 entry; trailing rules are
    multiplicative off a moving anchor (not mirror-exact) and have their
    own tests above."""
    rules = [([_rule("percentage", 10)], []), ([_rule("atr", 2)], []),
             ([], [_rule("percentage", 5)]), ([], [_rule("atr", 1)]),
             ([], [_rule("fixed", 80)]), ([_rule("fixed", 120)], [])]
    candles = [(98, 103, 94, 97), (92, 96, 88, 90), (95, 99, 91, 96), (112, 121, 110, 115)]
    for n, (sls, tps) in enumerate(rules):
        s_short = _exit_settings(sls, tps)
        mirror = lambda r: dict(r, value=200 - r["value"]) if r["type"] == "fixed" else r  # noqa: E731
        s_long = _exit_settings([mirror(r) for r in sls], [mirror(r) for r in tps])
        sp, lp = _spos(pid=100 + n), _lpos(pid=200 + n)
        for o, h, lo, c in candles:
            es = _short(bm, sp, c, h, lo, False, s_short, current_atr=3.0, row_open=o)
            el = bm._check_exits(lp, 200 - c, 200 - lo, 200 - h, False, s_long, current_atr=3.0, row_open=200 - o)
            assert ids(es) == ids(el), (sls, tps)
            for a, b in zip(es, el):
                assert a["price"] == pytest.approx(200 - b["price"])
            assert sp.highest_price == pytest.approx(200 - lp.highest_price)
            if es:
                break


def test_long_path_ignores_side_default(bm):
    """`side` defaults to long, so existing callers see exactly the old rules."""
    s = _exit_settings([_rule("percentage", 10)])
    ev = bm._check_exits(_lpos(), 92, 96, 89, False, s, row_open=95)
    assert ids(ev) == ["sl_0"] and ev[0]["price"] == pytest.approx(90)


# ── backtest ───────────────────────────────────────────────────────────────

def _bt_short_settings(leverage=2, sl=3, tp=3, amount_pct=40, cover=False, entry_always=False, capital=1000):
    s = _bt_settings(leverage=leverage, sl=sl, tp=tp, amount_pct=amount_pct, capital=capital)
    s["nodes"]["entry"] = {"class": "condition", "left": "close", "operator": ">" if entry_always else "<", "right": 0}
    s["nodes"]["short"] = {"class": "condition", "left": "close", "operator": ">", "right": 0}
    s["short_node"] = "short"
    if cover:
        s["nodes"]["cover"] = {"class": "condition", "left": "close", "operator": ">", "right": 0}
        s["cover_node"] = "cover"
    return s


def _run_backtest(db, settings, seed=11, name="bt-bot", drift=0.0005, n=400):
    insert_candles(db, make_candles(EXCHANGE, SWAP, TF, n, seed=seed, start_price=100.0, drift=drift))
    bot = BotConfig(name=name, is_active=True, is_sandbox=True, strategy="node_graph", settings=settings)
    db.add(bot)
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    bot = db.query(BotConfig).filter(BotConfig.name == name).one()
    positions = db.query(Position).filter(Position.bot_name == name, Position.mode == "backtest").order_by(Position.id).all()
    return bot, positions


def test_backtest_short_opens_sell_and_profits_on_falling_series(db):
    bot, positions = _run_backtest(db, _bt_short_settings(leverage=2, sl=50, tp=10), drift=-0.004, seed=3)
    assert positions and all(p.side == "short" for p in positions)
    first = positions[0]
    assert first.market_type == "swap" and first.leverage == 2.0
    # 40% of the pool as margin at 2x → notional 800 (fee on the notional fits)
    assert first.amount * first.entry_price == pytest.approx(0.4 * 1000 * 2, rel=1e-6)
    opens = db.query(Order).filter(Order.position_id == first.id, Order.side == "sell").all()
    closes = db.query(Order).filter(Order.position_id == first.id, Order.side == "buy").all()
    assert len(opens) == 1 and opens[0].reduce_only == 0 and opens[0].market_type == "swap"
    assert closes and all(o.reduce_only == 1 for o in closes)
    closed = [p for p in positions if p.status == "closed"]
    assert sum(p.profit_abs for p in closed) > 0
    # short PnL formula on the first closed take-profit: (entry − exit) × qty − fees
    p = next(p for p in closed if "tp_0" in (p.triggered_exits or []))
    buy = db.query(Order).filter(Order.position_id == p.id, Order.side == "buy").one()
    sell = db.query(Order).filter(Order.position_id == p.id, Order.side == "sell").one()
    expected = (p.entry_price - buy.price) * buy.amount - sell.fee - buy.fee
    assert p.profit_abs == pytest.approx(expected, rel=1e-6)
    assert p.profit_abs > 0
    summary = bot.settings["last_backtest_summary"]
    assert summary["short_trades"] == len(closed) and summary["long_trades"] == 0
    assert summary["market_type"] == "swap"


def test_backtest_short_slippage_is_against_the_trade(db):
    s = _bt_short_settings(leverage=1, sl=50, tp=500)
    s["trade_settings"]["entry"]["slippage"] = 1.0
    s["trade_settings"]["exit"]["slippage"] = 1.0
    _, positions = _run_backtest(db, s, seed=4, n=80)
    assert positions
    p = positions[0]
    first_close = db.query(Order).filter(Order.side == "sell", Order.position_id == p.id).one()
    # the short is sold 1% below the candle close
    from backend.models.candles import Candle
    c = db.query(Candle).filter(Candle.symbol == SWAP, Candle.timestamp == first_close.timestamp).one()
    assert p.entry_price == pytest.approx(c.close * 0.99, rel=1e-9)


def test_backtest_short_liquidation_on_high(db):
    lev = 10
    bot, positions = _run_backtest(db, _bt_short_settings(leverage=lev, sl=50, tp=500, amount_pct=20), seed=5, drift=0.004)
    liq = [p for p in positions if "liquidation" in (p.triggered_exits or [])]
    assert bot.settings["last_backtest_summary"]["liquidations"] >= 1 and liq
    p = liq[0]
    margin = p.entry_price * p.amount / lev
    assert p.profit_abs == pytest.approx(-margin, rel=1e-6)
    buy = db.query(Order).filter(Order.position_id == p.id, Order.side == "buy").one()
    assert buy.price == pytest.approx(pnl.liquidation_price("short", p.entry_price, lev), rel=1e-9)
    assert buy.price > p.entry_price and buy.fee == 0 and buy.reduce_only == 1


def test_backtest_buy_and_short_never_coexist(db):
    """Both signals every candle: the buy wins, the short is ignored, and no
    short ever opens while the long is on."""
    _, positions = _run_backtest(db, _bt_short_settings(leverage=2, entry_always=True), seed=6)
    assert positions and all(p.side == "long" for p in positions)


def test_backtest_cover_signal_flattens_short(db):
    """Cover fires on every candle: each short lives exactly one candle and
    closes via the strategy_cover exit at the close."""
    _, positions = _run_backtest(db, _bt_short_settings(leverage=2, sl=50, tp=500, cover=True), seed=7, n=60)
    closed = [p for p in positions if p.status == "closed"]
    assert closed and all("strategy_cover" in (p.triggered_exits or []) for p in closed)


def test_backtest_short_ignored_on_spot(db):
    s = _bt_short_settings(leverage=1)
    s.update({"market_type": "spot", "leverage": 1, "symbols": ["BTC/USDT"]})
    insert_candles(db, make_candles(EXCHANGE, "BTC/USDT", TF, 100, seed=8, start_price=100.0))
    bot = BotConfig(name="spot-short", is_active=True, is_sandbox=True, strategy="node_graph", settings=s)
    db.add(bot)
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    assert db.query(Position).filter(Position.bot_name == "spot-short").count() == 0


# ── live / paper ───────────────────────────────────────────────────────────

def test_live_short_entry_sells_non_reduce_only(db, swap_bot, run_tick):
    _, candles = swap_bot(_short_settings())
    close = candles[-1].close
    mock = SwapExchangeMock(free_quote=10_000.0, average=close)
    run_tick(mock)
    db.expire_all()
    assert mock.leverage_calls == [(3, SWAP, {"marginMode": "isolated"})]
    assert len(mock.created) == 1
    sell = mock.created[0]
    assert sell["side"] == "sell" and "reduceOnly" not in sell["params"]
    base_amount = 0.5 * 10_000 * 3 / close
    assert sell["amount"] == pytest.approx(base_amount / CONTRACT_SIZE, rel=1e-5)
    pos = _positions(db, status="open")
    assert len(pos) == 1 and pos[0].side == "short" and pos[0].market_type == "swap"
    assert pos[0].amount == pytest.approx(base_amount, rel=1e-5)
    order = db.query(Order).filter(Order.bot_name == "live-bot", Order.mode == "live").one()
    assert order.side == "sell" and not order.reduce_only


def test_live_short_stop_loss_buys_reduce_only(db, swap_bot, run_tick):
    _, candles = swap_bot(_short_settings(short_always=False, sl_pct=10))
    candles[-1].high = 120.0
    from backend.models.candles import Candle
    row = db.query(Candle).filter(Candle.symbol == SWAP, Candle.timestamp == candles[-1].timestamp).one()
    row.high = 120.0
    db.commit()
    pos = _open_short_position(db, candles, entry=100.0, amount=0.5)
    mock = SwapExchangeMock(average=110.0)
    run_tick(mock)
    db.expire_all()
    assert len(mock.created) == 1
    buy = mock.created[0]
    assert buy["side"] == "buy" and buy["params"] == {"reduceOnly": True}
    assert buy["amount"] == pytest.approx(0.5 / CONTRACT_SIZE)
    closed = db.get(Position, pos.id)
    assert closed.status == "closed"
    assert closed.profit_abs == pytest.approx((100.0 - 110.0) * 0.5)
    assert closed.profit_pct == pytest.approx(-10.0)
    close_order = db.query(Order).filter(Order.position_id == pos.id, Order.side == "buy").one()
    assert close_order.reduce_only == 1 and close_order.amount == pytest.approx(0.5)


def test_live_cover_signal_closes_short_at_close(db, swap_bot, run_tick):
    _, candles = swap_bot(_short_settings(short_always=False, cover=True, sl_pct=50))
    pos = _open_short_position(db, candles, entry=100.0, amount=0.5)
    mock = SwapExchangeMock(average=95.0)
    run_tick(mock)
    db.expire_all()
    assert [(o["side"], o["params"]) for o in mock.created] == [("buy", {"reduceOnly": True})]
    closed = db.get(Position, pos.id)
    assert closed.status == "closed" and "strategy_cover" in closed.triggered_exits
    assert closed.profit_abs == pytest.approx((100.0 - 95.0) * 0.5)
    # the cover signal does not reopen a short on the same tick (short node never fires)
    assert _positions(db, status="open") == []


def test_live_buy_signal_ignored_while_short_open(db, swap_bot, run_tick):
    _, candles = swap_bot(_short_settings(short_always=False, entry_always=True, sl_pct=50))
    _open_short_position(db, candles, entry=100.0, amount=0.5)
    mock = SwapExchangeMock(average=100.0)
    run_tick(mock)
    db.expire_all()
    assert mock.created == []
    open_pos = _positions(db, status="open")
    assert len(open_pos) == 1 and open_pos[0].side == "short"


def test_live_short_signal_ignored_while_long_open(db, swap_bot, run_tick):
    from tests.test_swap import _open_swap_position
    _, candles = swap_bot(_short_settings(sl_pct=50))
    _open_swap_position(db, candles, entry=100.0, amount=0.5)
    mock = SwapExchangeMock(average=100.0)
    run_tick(mock)
    db.expire_all()
    assert mock.created == []
    open_pos = _positions(db, status="open")
    assert len(open_pos) == 1 and open_pos[0].side == "long"


def test_force_close_all_covers_short(db, swap_bot, monkeypatch):
    from backend.models.exchange_keys import ExchangeKey
    from tests.test_live_tick import KEY_NAME
    bot, candles = swap_bot(_short_settings())
    pos = _open_short_position(db, candles, amount=0.5)
    bm = BotManager()
    mock = SwapExchangeMock(average=99.0)
    monkeypatch.setattr(bm, "_get_ccxt_instance", lambda key_record: mock)
    real = BotManager._reconcile_order
    monkeypatch.setattr(bm, "_reconcile_order",
                        lambda inst, order, sym, attempts=5, delay=1.0: real(bm, inst, order, sym, attempts, 0))
    key = db.query(ExchangeKey).filter(ExchangeKey.name == KEY_NAME).one()
    bm._close_all_open_positions(bot, db, {KEY_NAME: key})
    db.expire_all()
    assert [(o["side"], o["params"]) for o in mock.created] == [("buy", {"reduceOnly": True})]
    closed = db.get(Position, pos.id)
    assert closed.status == "closed" and closed.profit_abs == pytest.approx((100.0 - 99.0) * 0.5)
    assert closed.profit_pct == pytest.approx(1.0)


def test_forward_short_entry_and_pool_accounting(db, swap_bot, run_tick):
    s = _short_settings(leverage=4, amount_pct=50)
    s["api_execution"] = False
    s["api_key_name"] = None
    s["trade_settings"]["entry"]["slippage"] = 0.5
    _, candles = swap_bot(s)
    close = candles[-1].close
    run_tick(SwapExchangeMock())
    db.expire_all()
    pos = _positions(db, mode="forward_test", status="open")
    assert len(pos) == 1 and pos[0].side == "short"
    # short slippage: sold below the close
    assert pos[0].entry_price == pytest.approx(close * 0.995)
    assert pos[0].amount == pytest.approx(0.5 * 1000 * 4 / close, rel=1e-6)
    order = db.query(Order).filter(Order.bot_name == "live-bot", Order.mode == "forward_test").one()
    assert order.side == "sell" and order.reduce_only == 0


def test_reconciliation_compares_side(db, swap_bot):
    bot, candles = swap_bot(_short_settings())
    _open_short_position(db, candles, amount=0.5)
    bm = BotManager()
    ok = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "short", "contracts": 500, "contractSize": CONTRACT_SIZE}])
    assert bm._reconcile_positions_with_exchange(db, bot, ok, "live") == []
    flipped = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "long", "contracts": 500, "contractSize": CONTRACT_SIZE}])
    assert len(bm._reconcile_positions_with_exchange(db, bot, flipped, "live")) == 1
    smaller = SwapExchangeMock(positions=[{"symbol": SWAP, "side": "short", "contracts": 200, "contractSize": CONTRACT_SIZE}])
    assert len(bm._reconcile_positions_with_exchange(db, bot, smaller, "live")) == 1
    assert len(bm._reconcile_positions_with_exchange(db, bot, SwapExchangeMock(), "live")) == 1


# ── validator / fingerprint ────────────────────────────────────────────────

def test_validator_refuses_shorts_on_spot_and_warns_on_missing_cover():
    s = _short_settings(max_order_value=500)
    out = validate_bot_settings(dict(s), exchange_id="binance", key_market_type="swap")
    assert out["errors"] == []
    assert any("cover" in w.lower() or "Close short" in w for w in out["warnings"])
    spot = _short_settings(max_order_value=500)
    spot.update({"market_type": "spot", "leverage": 1, "symbols": ["BTC/USDT"]})
    out = validate_bot_settings(dict(spot), exchange_id="binance", key_market_type="spot")
    assert any("Shorts need a perpetual market" in e for e in out["errors"])
    missing = _short_settings(max_order_value=500)
    missing["short_node"] = "nope"
    assert any("short_node" in e or "nope" in e for e in validate_bot_settings(dict(missing), exchange_id="binance")["errors"])
    # short leg SL/TP validated like the entry leg
    bad_leg = _short_settings(max_order_value=500)
    bad_leg["trade_settings"]["short"] = {"amount_type": "percentage", "amount_value": 50, "fee": 0.1, "slippage": 0,
                                         "stop_losses": [{"type": "bogus", "value": 5}], "take_profits": []}
    assert any(e.startswith("Short Stop loss") for e in validate_bot_settings(dict(bad_leg), exchange_id="binance")["errors"])


def test_fingerprint_ignores_absent_short_nodes():
    base = _swap_settings()
    explicit = dict(base, short_node=None, cover_node=None)
    assert _config_fingerprint(explicit) == _config_fingerprint(base)
    assert _config_fingerprint(dict(base, short_node="short")) != _config_fingerprint(base)
