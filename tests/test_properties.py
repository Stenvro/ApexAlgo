"""Property tests (hypothesis) for the pure trade-economics seams:
``pnl.price_pnl`` / ``pnl.liquidation_price``, the entry sizing chain the
backtest runs (``calculate_trade_amount`` → ``max_order_value`` cap →
``max_affordable_amount``), and the long/short mirror of ``exits.check_exits``
for the additively symmetric rule types (percentage, ATR, fixed).

Trailing rules are deliberately not part of the mirror property: they are
multiplicative off a moving anchor, so the reflection p → 2c − p does not map a
long trail onto the short trail (covered by explicit cases in test_shorts.py).
"""
import math
import threading

import pytest
from hypothesis import assume, given, settings, strategies as st

from backend.engine import exits, pnl
from backend.engine.backtest import _cap_by_max_order_value
from backend.engine.sizing import calculate_trade_amount, max_affordable_amount
from backend.models.positions import Position

ENTRY = 100.0  # reflection centre for the exit mirror
REL = 1e-9

prices = st.floats(min_value=0.01, max_value=1e5, allow_nan=False, allow_infinity=False)
amounts = st.floats(min_value=1e-8, max_value=1e6, allow_nan=False, allow_infinity=False)
leverages = st.floats(min_value=1.0, max_value=125.0, allow_nan=False, allow_infinity=False)
sides = st.sampled_from(list(pnl.SIDES))


# ── pnl.price_pnl ──────────────────────────────────────────────────────────

@given(sides, prices, prices, amounts)
def test_price_pnl_sign_follows_the_direction(side, entry, exit_, amount):
    got = pnl.price_pnl(side, entry, exit_, amount)
    move = exit_ - entry
    if move == 0:
        assert got == 0
    else:
        assert math.copysign(1, got) == pnl.direction(side) * math.copysign(1, move)
    # magnitude is the price move on the full amount, regardless of side
    assert abs(got) == pytest.approx(abs(move) * amount, rel=REL)


@given(prices, prices, amounts)
def test_price_pnl_is_antisymmetric_in_side(entry, exit_, amount):
    assert pnl.price_pnl("long", entry, exit_, amount) == -pnl.price_pnl("short", entry, exit_, amount)


@given(sides, prices, prices, amounts, amounts)
def test_price_pnl_is_linear_in_amount(side, entry, exit_, a, b):
    whole = pnl.price_pnl(side, entry, exit_, a + b)
    parts = pnl.price_pnl(side, entry, exit_, a) + pnl.price_pnl(side, entry, exit_, b)
    assert whole == pytest.approx(parts, rel=1e-9, abs=1e-9)


# ── pnl.liquidation_price ──────────────────────────────────────────────────

@given(sides, prices, leverages)
def test_liquidation_price_lies_on_the_losing_side_of_entry(side, entry, lev):
    liq = pnl.liquidation_price(side, entry, lev)
    if side == "long" and lev <= 1:
        assert liq is None  # a 1x long can only lose its margin
        return
    assert liq is not None and liq > 0
    if side == "long":
        assert liq < entry
    else:
        assert liq > entry
    # the level is exactly the price where the loss equals (1 − MMR) × margin
    margin = entry / lev
    assert -pnl.price_pnl(side, entry, liq, 1.0) == pytest.approx((1 - pnl.MAINTENANCE_MARGIN) * margin, rel=REL)
    # and `liquidated` agrees with the level on a candle that just touches it
    assert pnl.liquidated(side, liq, row_high=max(entry, liq), row_low=min(entry, liq))
    assert not pnl.liquidated(side, liq, row_high=entry, row_low=entry)


@given(sides, prices, leverages, leverages)
def test_liquidation_price_moves_toward_entry_with_more_leverage(side, entry, lev_a, lev_b):
    lo, hi = sorted((lev_a, lev_b))
    if side == "long" and lo <= 1:
        lo = 1.0000001  # 1x long has no level; compare strictly leveraged longs only
        if hi <= lo:
            return
    liq_lo = pnl.liquidation_price(side, entry, lo)
    liq_hi = pnl.liquidation_price(side, entry, hi)
    dist_lo, dist_hi = abs(entry - liq_lo), abs(entry - liq_hi)
    if hi == lo:
        assert dist_lo == dist_hi
    else:
        assert dist_hi <= dist_lo  # more leverage → liquidation closer to entry
        # monotone in the price too: a long's level rises, a short's falls
        assert (liq_hi >= liq_lo) if side == "long" else (liq_hi <= liq_lo)


# ── entry sizing never exceeds the pool ────────────────────────────────────

@st.composite
def sizing_inputs(draw):
    return dict(
        price=draw(st.floats(min_value=0.001, max_value=1e5, allow_nan=False, allow_infinity=False)),
        equity=draw(st.floats(min_value=0.0, max_value=1e7, allow_nan=False, allow_infinity=False)),
        pct=draw(st.one_of(st.none(), st.floats(min_value=0.0, max_value=100.0, allow_nan=False))),
        amount_type=draw(st.sampled_from(["percentage", "fixed"])),
        leverage=draw(leverages),
        fee=draw(st.floats(min_value=0.0, max_value=0.01, allow_nan=False)),
        slippage=draw(st.floats(min_value=0.0, max_value=0.05, allow_nan=False)),
        cap=draw(st.one_of(st.just(0), st.floats(min_value=1.0, max_value=1e6, allow_nan=False))),
        side=draw(sides),
    )


@given(sizing_inputs())
@settings(max_examples=300)
def test_backtest_entry_sizing_fits_the_pool_and_the_order_cap(p):
    """The sizing chain the backtest/forward tick applies to every entry:
    `calculate_trade_amount` (share of the pool, or a fixed cash amount, times
    leverage) → `max_order_value` cap on the notional → for percentage sizing
    a clamp so margin + entry fee at the slipped fill still fit the pool.
    Whatever comes out, `pnl.locked_capital` never exceeds the pool and the
    notional never exceeds the cap."""
    bot_settings = {
        "backtest_capital": 1000,
        "max_order_value": p["cap"],
        "trade_settings": {"entry": {"amount_type": p["amount_type"], "amount_value": p["pct"]}},
    }
    lev = p["leverage"]
    amount = calculate_trade_amount(p["price"], bot_settings, current_equity=p["equity"], leverage=lev, side=p["side"])
    if p["equity"] <= 0 and p["amount_type"] == "percentage":
        assert amount is None  # depleted pool: no entry at all
        return
    assert amount is not None and amount > 0
    amount = _cap_by_max_order_value(amount, p["price"], bot_settings)
    if p["cap"]:
        assert amount * p["price"] <= p["cap"] * (1 + REL)

    dir_ = pnl.direction(p["side"])
    fill = p["price"] * (1 + dir_ * p["slippage"])  # slippage works against the taker
    if p["amount_type"] == "percentage":
        amount = min(amount, max_affordable_amount(p["equity"], fill, lev, p["fee"]))
        locked = pnl.locked_capital(fill, amount, lev, p["fee"])
        assert locked <= p["equity"] * (1 + REL) + 1e-12
        # a share of the pool means the share: at most `pct` of the free cash
        # goes into margin before frictions (the 0.0001 floor only kicks in
        # when the share is dust, and the clamp above catches that)
        share = (p["pct"] if p["pct"] else 100.0) / 100
        assert pnl.locked_capital(p["price"], amount, lev) <= max(p["equity"] * share, 0.0001 * p["price"] / lev) * (1 + REL) + 1e-12
    else:
        # fixed sizing puts up `amount_value` as margin: the notional is
        # `leverage` × that, whatever the pool (the backtest gate rejects
        # entries that do not fit; that gate is not part of sizing)
        cash = p["pct"] if p["pct"] else 100.0
        want = max(cash / p["price"] * lev, 0.0001)
        if p["cap"] and want * p["price"] > p["cap"]:
            want = p["cap"] / p["price"]
        assert amount == pytest.approx(want, rel=REL)
    # the cap survives the affordability clamp (it can only shrink the order)
    if p["cap"]:
        assert amount * p["price"] <= p["cap"] * (1 + REL)


# ── check_exits: long/short mirror under p → 2·ENTRY − p ───────────────────

def _rule(kind, value):
    return {"type": kind, "value": value, "close_amount_type": "percentage", "close_amount_value": 100}


def _settings_for(sls, tps):
    return {"trade_settings": {"entry": {"stop_losses": list(sls), "take_profits": list(tps)}, "exit": {}}}


def _mirror_rule(r):
    return dict(r, value=2 * ENTRY - r["value"]) if r["type"] == "fixed" else r


def _pos(side, pid):
    return Position(id=pid, exchange="binance", bot_name="prop", symbol="BTC/USDT", mode="backtest",
                    status="open", side=side, entry_price=ENTRY, amount=1.0, highest_price=None, triggered_exits=None)


@st.composite
def candle_paths(draw):
    """OHLC-consistent candles with every price strictly inside (0, 2·ENTRY)
    so the reflection stays positive."""
    n = draw(st.integers(min_value=1, max_value=6))
    out = []
    for _ in range(n):
        lo = draw(st.floats(min_value=1.0, max_value=198.0, allow_nan=False))
        hi = draw(st.floats(min_value=lo, max_value=199.0, allow_nan=False))
        o = draw(st.floats(min_value=lo, max_value=hi, allow_nan=False))
        c = draw(st.floats(min_value=lo, max_value=hi, allow_nan=False))
        out.append((o, hi, lo, c))
    return out


pct_rule = st.builds(_rule, st.just("percentage"), st.floats(min_value=0.1, max_value=90.0, allow_nan=False))
atr_rule = st.builds(_rule, st.just("atr"), st.floats(min_value=0.1, max_value=10.0, allow_nan=False))
fixed_sl = st.builds(_rule, st.just("fixed"), st.floats(min_value=1.0, max_value=ENTRY - 0.5, allow_nan=False))
fixed_tp = st.builds(_rule, st.just("fixed"), st.floats(min_value=ENTRY + 0.5, max_value=199.0, allow_nan=False))
# Long-side rule sets; the short side gets the mirrored fixed levels. Fixed
# SL/TP are placed on the long side of the entry (below/above), the only
# placement where they are stop/target rules at all.
sl_rules = st.lists(st.one_of(pct_rule, atr_rule, fixed_sl), min_size=0, max_size=3)
tp_rules = st.lists(st.one_of(pct_rule, atr_rule, fixed_tp), min_size=0, max_size=3)


def _touches_a_level(path, sls, tps, atr, eps=1e-6):
    """True when a candle price (or its reflection) is within `eps` of any
    level the long/short rules could evaluate on this path: the fixed levels,
    the entry-relative percentage levels and the ATR levels off every
    running extreme the path produces."""
    anchors = {ENTRY}
    peak = ENTRY
    for _, h, _, _ in path:
        peak = max(peak, h)
        anchors.add(peak)
    levels = set()
    for r in list(sls) + list(tps):
        if r["type"] == "fixed":
            levels.add(r["value"])
        elif r["type"] == "percentage":
            levels.update({ENTRY * (1 - r["value"] / 100), ENTRY * (1 + r["value"] / 100)})
        elif r["type"] == "atr":
            levels.update({a - r["value"] * atr for a in anchors} | {a + r["value"] * atr for a in anchors})
    levels |= {2 * ENTRY - lv for lv in list(levels)}
    prices = {v for candle in path for v in candle}
    prices |= {2 * ENTRY - v for v in list(prices)}
    return any(abs(p - lv) <= eps for p in prices for lv in levels)


@given(sl_rules, tp_rules, candle_paths(), st.floats(min_value=0.0, max_value=20.0, allow_nan=False), st.booleans())
@settings(max_examples=300)
def test_short_exits_are_the_reflection_of_long_exits(sls, tps, path, atr, signal):
    """Running the long rules on a path and the short rules (fixed levels
    reflected) on the reflected path must produce the same event ids, the
    same quantities, reflected fill prices and a reflected extreme — candle
    by candle, including the state carried between candles."""
    # The reflection 2·ENTRY − p is not exact in floating point, so a price
    # that sits within ~1e-13 of a trigger level can be "touched" on one side
    # and missed on the other. Those examples are a rounding artefact, not a
    # rule asymmetry: skip them (rules keep their own explicit boundary tests).
    assume(not _touches_a_level(path, sls, tps, atr))
    long_cfg = _settings_for(sls, tps)
    short_cfg = _settings_for([_mirror_rule(r) for r in sls], [_mirror_rule(r) for r in tps])
    states, lock = {}, threading.Lock()
    lp, sp = _pos("long", 1), _pos("short", 2)
    for o, h, lo, c in path:
        el = exits.check_exits(states, lock, lp, c, h, lo, signal, long_cfg, atr, o, side="long")
        es = exits.check_exits(states, lock, sp, 2 * ENTRY - c, 2 * ENTRY - lo, 2 * ENTRY - h, signal, short_cfg, atr,
                               2 * ENTRY - o, side="short")
        # ids match up to the side-specific strategy exit name
        norm = lambda e: "strategy" if e["id"].startswith("strategy_") else e["id"]  # noqa: E731
        assert [norm(e) for e in es] == [norm(e) for e in el]
        assert [e["reason"] for e in es] == [e["reason"] for e in el]
        assert [e["qty_pct"] for e in es] == [e["qty_pct"] for e in el]
        for a, b in zip(es, el):
            assert a["price"] == pytest.approx(2 * ENTRY - b["price"], rel=REL, abs=1e-9)
        assert sp.highest_price == pytest.approx(2 * ENTRY - lp.highest_price, rel=REL, abs=1e-9)
        # long-only ids on the long side, cover id on the short side
        assert all(e["id"] != "strategy_cover" for e in el) and all(e["id"] != "strategy_sell" for e in es)
        if el:
            break
