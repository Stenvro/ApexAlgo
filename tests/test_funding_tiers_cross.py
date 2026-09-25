"""v2.3: funding settlements, tiered maintenance margin and cross margin in
the backtest and the forward test. Everything comes from the stored
`funding_rates` / `leverage_tiers` tables (the conftest fixture keeps the
exchange out of it)."""
from datetime import timedelta

import pytest

from backend.engine import funding, tiers
from backend.engine.bot_manager import BotManager
from backend.engine.contracts import MAINTENANCE_MARGIN, spec_from_symbol
from backend.models.bots import BotConfig
from backend.models.market_data import FundingRate, LeverageTier
from backend.models.orders import Order
from backend.models.positions import Position
from tests.conftest import insert_candles, make_candles
from tests.test_live_tick import EXCHANGE, TF
from tests.test_swap import (SWAP, SwapExchangeMock, _bt_settings, _forward_swap_settings, _open_forward_swap_position,
                             run_tick, swap_bot)  # noqa: F401 (fixtures)

SPEC = spec_from_symbol(SWAP)


# ── unit rules ─────────────────────────────────────────────────────────────

def test_payment_sign_follows_side_and_rate():
    # positive rate: the long pays 0.01% of its notional, the short receives it
    assert funding.payment(SPEC, "long", 2.0, 100.0, 0.0001) == pytest.approx(-0.02)
    assert funding.payment(SPEC, "short", 2.0, 100.0, 0.0001) == pytest.approx(0.02)
    assert funding.payment(SPEC, "long", 2.0, 100.0, -0.0001) == pytest.approx(0.02)


def test_settlements_are_the_half_open_window():
    from datetime import datetime
    ev = [(datetime(2023, 1, 1, h), 0.0001) for h in (0, 8, 16)]
    assert funding.settlements(ev, datetime(2023, 1, 1, 0), datetime(2023, 1, 1, 16)) == ev[1:]
    assert funding.settlements(ev, datetime(2023, 1, 1, 16), datetime(2023, 1, 1, 23)) == []
    assert funding.coverage(ev, None, None)["funding"] == "simulated"
    assert funding.coverage([], None, None) == {"funding": "no data", "funding_events": 0}


def test_mmr_for_picks_the_bracket_and_falls_back_flat():
    assert tiers.mmr_for([], 5000) == MAINTENANCE_MARGIN
    brackets = [(0.0, 10000.0, 0.004), (10000.0, 50000.0, 0.01), (50000.0, None, 0.025)]
    assert tiers.mmr_for(brackets, 5000) == 0.004
    assert tiers.mmr_for(brackets, 10000) == 0.01
    assert tiers.mmr_for(brackets, 1e9) == 0.025
    assert tiers.tier_size(SPEC, 2.0, 100.0) == pytest.approx(200.0)
    # a higher maintenance rate moves the long's liquidation level up (closer)
    assert SPEC.liquidation_price("long", 100.0, 5, mmr=0.025) > SPEC.liquidation_price("long", 100.0, 5)


# ── backtest ───────────────────────────────────────────────────────────────

def _seed_funding(db, candles, rate, every=8):
    """One settlement every `every` candles of the series, at the candle close."""
    rows = [FundingRate(exchange=EXCHANGE, symbol=SWAP, timestamp=c.timestamp, rate=rate)
            for i, c in enumerate(candles) if i % every == 0]
    db.add_all(rows)
    db.commit()
    return rows


def _run(db, settings, seed=11, name="bt-bot", candles=None):
    if candles is None:
        candles = make_candles(EXCHANGE, SWAP, TF, 400, seed=seed, start_price=100.0)
        insert_candles(db, candles)
    bot = BotConfig(name=name, is_active=True, is_sandbox=True, strategy="node_graph", settings=settings)
    db.add(bot)
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    bot = db.query(BotConfig).filter(BotConfig.name == name).one()
    positions = db.query(Position).filter(Position.bot_name == name, Position.mode == "backtest").order_by(Position.id).all()
    return bot, positions, candles


def test_backtest_charges_stored_funding_on_open_positions(db):
    candles = make_candles(EXCHANGE, SWAP, TF, 400, seed=11, start_price=100.0)
    insert_candles(db, candles)
    bot0, base, _ = _run(db, _bt_settings(leverage=3), name="no-funding", candles=candles)
    _seed_funding(db, candles, rate=0.001)  # 0.1% per settlement: a long pays
    bot1, paid, _ = _run(db, _bt_settings(leverage=3), name="with-funding", candles=candles)
    s0, s1 = bot0.settings["last_backtest_summary"], bot1.settings["last_backtest_summary"]
    assert s0["funding"] == "no data" and s0["funding_paid"] == 0
    assert s1["funding"] == "simulated" and s1["funding_events"] > 0 and s1["funding_paid"] < 0
    # the same trades (funding moves the pool, so percentage sizing differs slightly — not the signals)
    o0 = [(o.timestamp, o.side) for o in db.query(Order).filter(Order.bot_name == "no-funding").order_by(Order.id)]
    o1 = [(o.timestamp, o.side) for o in db.query(Order).filter(Order.bot_name == "with-funding").order_by(Order.id)]
    assert o0 == o1
    # every long that spanned a settlement paid; profit_abs carries it, the summary sums it
    charged = [p for p in paid if p.funding_paid]
    assert charged and all(p.funding_paid < 0 for p in charged)
    assert sum(p.funding_paid or 0.0 for p in paid) == pytest.approx(s1["funding_paid"])
    assert s1["net_pnl"] < s0["net_pnl"]
    for p in paid:
        assert p.funding_until is not None
    # a charged position spanned at least one settlement; one that spanned none paid nothing
    rates = [r.timestamp for r in db.query(FundingRate).all()]
    for p in paid:
        n = sum(1 for r in rates if p.created_at < r <= p.closed_at)
        assert (n >= 1) == bool(p.funding_paid)


def test_backtest_uses_the_stored_tier_rate_for_liquidation(db):
    """A tier with a 20% maintenance rate liquidates the 10x long on a move
    that the flat 0.5% rate survives."""
    candles = make_candles(EXCHANGE, SWAP, TF, 400, seed=5, start_price=100.0)
    insert_candles(db, candles)
    settings = _bt_settings(leverage=10, sl=50, tp=200, amount_pct=20)
    bot0, flat, _ = _run(db, settings, name="flat", candles=candles)
    db.add(LeverageTier(exchange=EXCHANGE, symbol=SWAP, tier=1, min_size=0, max_size=None, mmr=0.2, max_leverage=10))
    db.commit()
    bot1, tiered, _ = _run(db, settings, name="tiered", candles=candles)
    assert bot0.settings["last_backtest_summary"]["mmr_source"] == "flat"
    assert bot1.settings["last_backtest_summary"]["mmr_source"] == "tiers"
    assert bot1.settings["last_backtest_summary"]["liquidations"] >= bot0.settings["last_backtest_summary"]["liquidations"]
    liq = [o for o in db.query(Order).filter(Order.bot_name == "tiered", Order.side == "sell", Order.fee == 0)]
    for o in liq:
        p = db.get(Position, o.position_id)
        assert o.price == pytest.approx(SPEC.liquidation_price("long", p.entry_price, 10, mmr=0.2))


def _cross_settings(leverage=10):
    s = _bt_settings(leverage=leverage, sl=90, tp=300, amount_pct=50)
    s["margin_mode"] = "cross"
    return s


def test_cross_margin_liquidates_the_account_and_drains_the_wallet(db):
    """A crash candle: isolated loses the position's margin, cross loses the
    position's margin *and* the free cash (booked on the position, so
    profit_pct < −100 and the pool ends at zero)."""
    candles = make_candles(EXCHANGE, SWAP, TF, 60, seed=3, start_price=100.0, vol=0.001, drift=0.0)
    crash = candles[30]
    crash.low = crash.open * 0.5
    crash.close = crash.open * 0.55
    insert_candles(db, candles)
    iso = _cross_settings(); iso["margin_mode"] = "isolated"; iso["backtest_lookback"] = 60
    cro = _cross_settings(); cro["backtest_lookback"] = 60
    bot_i, pos_i, _ = _run(db, iso, name="iso", candles=candles)
    bot_c, pos_c, _ = _run(db, cro, name="cross", candles=candles)
    si, sc = bot_i.settings["last_backtest_summary"], bot_c.settings["last_backtest_summary"]
    assert si["liquidations"] >= 1 and sc["liquidations"] >= 1
    assert sc["margin_mode"] == "cross"
    liq_i = next(p for p in pos_i if "liquidation" in (p.triggered_exits or []))
    liq_c = next(p for p in pos_c if "liquidation" in (p.triggered_exits or []))
    assert liq_i.profit_pct == pytest.approx(-100.0)
    assert liq_c.profit_pct < -100.0
    assert liq_c.profit_abs < liq_i.profit_abs
    # the whole pool is gone: margin + entry fee + the free cash
    assert sc["net_pnl"] == pytest.approx(-1000.0, abs=0.02)
    assert any("cross-margin liquidation" in line["msg"] for line in _console(bot_c.name))


def test_cross_margin_survives_a_move_that_liquidates_isolated(db):
    """With half the capital free, a cross account survives the move that
    liquidates the isolated position: the free cash backs the position."""
    candles = make_candles(EXCHANGE, SWAP, TF, 60, seed=3, start_price=100.0, vol=0.001, drift=0.0)
    dip = candles[30]
    dip.low = dip.open * 0.88   # 12% down: isolated 10x (~9.95%) is gone, cross (margin+cash ≈ 20%) is not
    dip.close = dip.open * 0.95
    insert_candles(db, candles)
    iso = _cross_settings(); iso["margin_mode"] = "isolated"; iso["backtest_lookback"] = 60
    cro = _cross_settings(); cro["backtest_lookback"] = 60
    bot_i, _, _ = _run(db, iso, name="iso", candles=candles)
    bot_c, _, _ = _run(db, cro, name="cross", candles=candles)
    assert bot_i.settings["last_backtest_summary"]["liquidations"] >= 1
    assert bot_c.settings["last_backtest_summary"]["liquidations"] == 0


def _console(bot_name):
    from backend.core import bot_log_buffer as blb
    return blb.get_logs(bot_name)


# ── forward test parity ────────────────────────────────────────────────────

def test_forward_tick_charges_funding_like_the_backtest(db, swap_bot, run_tick):  # noqa: F811
    _, candles = swap_bot(_forward_swap_settings(leverage=10, sl_pct=50))
    pos = _open_forward_swap_position(db, candles, entry=100.0, amount=1.0, leverage=10.0, fee=0.1)
    # two settlements between the opening candle and the processed one, one before it (must not count)
    ts_open = candles[-2].timestamp
    ts_last = candles[-1].timestamp
    db.add_all([
        FundingRate(exchange=EXCHANGE, symbol=SWAP, timestamp=ts_open - timedelta(hours=1), rate=0.01),
        FundingRate(exchange=EXCHANGE, symbol=SWAP, timestamp=ts_open + (ts_last - ts_open) / 2, rate=0.001),
        FundingRate(exchange=EXCHANGE, symbol=SWAP, timestamp=ts_last, rate=-0.0005),
    ])
    db.commit()
    bm = run_tick(SwapExchangeMock())
    db.expire_all()
    p = db.get(Position, pos.id)
    close = float(candles[-1].close)
    expected = -(0.001 * 1.0 * close) + (0.0005 * 1.0 * close)
    assert p.status == "open"
    assert p.funding_paid == pytest.approx(expected)
    assert p.profit_abs == pytest.approx(expected)
    assert p.profit_pct == pytest.approx(100 * expected / (100.0 / 10 + 0.1))
    assert p.funding_until == ts_last
    # forward pool moved by the same amount; the drawdown curve too
    from backend.engine.sizing import forward_pool
    bot = db.query(BotConfig).filter(BotConfig.name == "live-bot").one()
    assert forward_pool(db, bot, "USDT") == pytest.approx(1000 - 100.0 / 10 - 0.1 + expected)
    dd = bm._drawdown_cache.get(("live-bot", "forward"))
    if dd is not None:
        assert dd["running_pnl"] == pytest.approx(expected)
    # a second tick on the same candle charges nothing twice
    run_tick(SwapExchangeMock())
    db.expire_all()
    assert db.get(Position, pos.id).funding_paid == pytest.approx(expected)


def test_forward_tick_uses_the_stored_tier_rate(db, swap_bot, run_tick):  # noqa: F811
    """10x long, candle low at −9.7%: the flat 0.5% level (−9.95%) holds,
    a 5% tier rate (−9.5%) liquidates."""
    _, candles = swap_bot(_forward_swap_settings(leverage=10, sl_pct=50), last_low=90.3)
    pos = _open_forward_swap_position(db, candles, entry=100.0, amount=1.0, leverage=10.0, fee=0.1)
    db.add(LeverageTier(exchange=EXCHANGE, symbol=SWAP, tier=1, min_size=0, max_size=None, mmr=0.05, max_leverage=10))
    db.commit()
    run_tick(SwapExchangeMock())
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "closed" and "liquidation" in (p.triggered_exits or [])
    sell = db.query(Order).filter(Order.position_id == pos.id, Order.side == "sell").one()
    assert sell.price == pytest.approx(SPEC.liquidation_price("long", 100.0, 10, mmr=0.05))


def test_forward_cross_liquidation_books_the_free_cash(db, swap_bot, run_tick):  # noqa: F811
    """Cross forward bot, 10x long on a 1500 notional (150 margin out of
    1000): a 95% crash costs more than the whole account; the position books
    margin + fee + free cash and the pool ends at zero."""
    s = _forward_swap_settings(leverage=10, sl_pct=99)  # the stop sits below the crash low
    s["margin_mode"] = "cross"
    _, candles = swap_bot(s, last_low=5.0)
    pos = _open_forward_swap_position(db, candles, entry=100.0, amount=15.0, leverage=10.0, fee=0.1)
    run_tick(SwapExchangeMock())
    db.expire_all()
    p = db.get(Position, pos.id)
    assert p.status == "closed" and "liquidation" in (p.triggered_exits or [])
    free_cash = 1000 - 1500.0 / 10 - 0.1
    assert p.profit_abs == pytest.approx(-(1500.0 / 10 + 0.1 + free_cash))
    assert p.profit_pct < -100.0
    from backend.engine.sizing import forward_pool
    bot = db.query(BotConfig).filter(BotConfig.name == "live-bot").one()
    assert forward_pool(db, bot, "USDT") == pytest.approx(0.0, abs=1e-6)


def test_forward_cross_holds_where_isolated_liquidates(db, swap_bot, run_tick):  # noqa: F811
    s = _forward_swap_settings(leverage=10, sl_pct=95)
    s["margin_mode"] = "cross"
    _, candles = swap_bot(s, last_low=88.0)  # −12%: isolated 10x is gone, the cross account (−180 on 1000) is not
    pos = _open_forward_swap_position(db, candles, entry=100.0, amount=15.0, leverage=10.0, fee=0.1)
    run_tick(SwapExchangeMock())
    db.expire_all()
    assert db.get(Position, pos.id).status == "open"


def test_validator_warns_on_cross():
    from backend.engine.settings_validator import validate_bot_settings
    s = _cross_settings()
    s["api_execution"] = False
    out = validate_bot_settings(s, exchange_id="binance")
    assert out["errors"] == [] and any("cross" in w for w in out["warnings"])
