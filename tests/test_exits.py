"""Table tests for ``BotManager._check_exits`` — the exit rules shared by the
backtest and the live loop. Positions are plain (unpersisted) ORM objects; the
manager's in-memory ``position_states`` is what the rules read and update."""
import pytest

from backend.engine.bot_manager import BotManager
from backend.models.positions import Position


def _pos(entry=100.0, amount=1.0, pid=1, highest=None, triggered=None):
    return Position(id=pid, exchange="binance", bot_name="t", symbol="BTC/USDT", mode="backtest",
                    status="open", side="long", entry_price=entry, amount=amount,
                    highest_price=highest, triggered_exits=triggered)


def _settings(stop_losses=(), take_profits=(), exit_amount=None):
    exit_cfg = {"amount_type": "percentage", "amount_value": exit_amount} if exit_amount is not None else {}
    return {"trade_settings": {"entry": {"stop_losses": list(stop_losses), "take_profits": list(take_profits)},
                               "exit": exit_cfg}}


def _sl(kind, value, close_pct=100):
    return {"type": kind, "value": value, "close_amount_type": "percentage", "close_amount_value": close_pct}


def _tp(kind, value, close_pct=100):
    return {"type": kind, "value": value, "close_amount_type": "percentage", "close_amount_value": close_pct}


@pytest.fixture
def bm():
    return BotManager()


def ids(events):
    return [e["id"] for e in events]


# ── stop losses ────────────────────────────────────────────────────────────

def test_percentage_sl_fills_at_trigger(bm):
    ev = bm._check_exits(_pos(), 92, 96, 89, False, _settings([_sl("percentage", 10)]), row_open=95)
    assert ids(ev) == ["sl_0"]
    assert ev[0]["reason"] == "stop_loss"
    assert ev[0]["price"] == pytest.approx(90.0)
    assert ev[0]["qty_pct"] == 100


def test_gap_below_sl_fills_at_open(bm):
    ev = bm._check_exits(_pos(), 84, 86, 82, False, _settings([_sl("percentage", 10)]), row_open=85)
    assert ev[0]["price"] == pytest.approx(85.0)


def test_sl_not_hit_when_low_stays_above_trigger(bm):
    ev = bm._check_exits(_pos(), 95, 99, 91, False, _settings([_sl("percentage", 10)]), row_open=98)
    assert ev == []


def test_trailing_sl_anchors_to_previous_peak_not_current_candle(bm):
    pos = _pos()
    cfg = _settings([_sl("trailing", 15)])
    # One candle cannot both raise the trail and trigger against its own low:
    # high 130 would put the trail at 110.5, but the anchor is still the entry (100 → 85)
    assert bm._check_exits(pos, 128, 130, 100, False, cfg, row_open=100) == []
    assert pos.highest_price == 130
    # Next candle: trail is 130 × 0.85 = 110.5 → low 105 triggers, filled at the trigger
    ev = bm._check_exits(pos, 106, 120, 105, False, cfg, row_open=118)
    assert ids(ev) == ["sl_0"]
    assert ev[0]["price"] == pytest.approx(110.5)


def test_trailing_sl_restores_persisted_peak_from_db(bm):
    # After a restart position_states is empty; the DB row carries the peak
    pos = _pos(highest=150.0)
    ev = bm._check_exits(pos, 126, 130, 125, False, _settings([_sl("trailing", 15)]), row_open=129)
    assert ids(ev) == ["sl_0"]
    assert ev[0]["price"] == pytest.approx(127.5)  # 150 × 0.85


def test_atr_sl_skipped_when_atr_is_zero(bm):
    cfg = _settings([_sl("atr", 2.0)])
    assert bm._check_exits(_pos(), 60, 61, 50, False, cfg, current_atr=0.0, row_open=60) == []
    ev = bm._check_exits(_pos(pid=2), 93, 96, 92, False, cfg, current_atr=3.0, row_open=95)
    assert ids(ev) == ["sl_0"]
    assert ev[0]["price"] == pytest.approx(94.0)  # 100 − 2 × 3


def test_fixed_sl_is_an_absolute_price(bm):
    ev = bm._check_exits(_pos(), 70, 80, 69, False, _settings([_sl("fixed", 70)]), row_open=75)
    assert ev[0]["price"] == pytest.approx(70.0)


def test_invalid_or_zero_sl_is_ignored(bm):
    cfg = _settings([_sl("bogus", 10), _sl("percentage", 0), {"type": "percentage", "value": "abc"}])
    assert bm._check_exits(_pos(), 50, 55, 40, False, cfg, row_open=50) == []


# ── take profits ───────────────────────────────────────────────────────────

def test_gap_above_tp_fills_at_open(bm):
    ev = bm._check_exits(_pos(), 111, 112, 109, False, _settings(take_profits=[_tp("percentage", 5)]), row_open=110)
    assert ids(ev) == ["tp_0"]
    assert ev[0]["reason"] == "take_profit"
    assert ev[0]["price"] == pytest.approx(110.0)


def test_multiple_tps_ordered_best_price_first(bm):
    cfg = _settings(take_profits=[_tp("percentage", 5, 50), _tp("percentage", 10, 50)])
    ev = bm._check_exits(_pos(), 111, 112, 100, False, cfg, row_open=100)
    assert ids(ev) == ["tp_1", "tp_0"]
    assert [e["price"] for e in ev] == [pytest.approx(110.0), pytest.approx(105.0)]
    assert [e["qty_pct"] for e in ev] == [50, 50]


def test_triggered_tp_is_not_fired_twice(bm):
    pos = _pos()
    cfg = _settings(take_profits=[_tp("percentage", 5, 50), _tp("percentage", 10, 50)])
    first = bm._check_exits(pos, 106, 107, 100, False, cfg, row_open=100)
    assert ids(first) == ["tp_0"]
    # The engine records the fired id in position_states after booking the sell
    bm.position_states[pos.id]["triggered_exits"].add("tp_0")
    second = bm._check_exits(pos, 106, 107, 100, False, cfg, row_open=100)
    assert second == []
    third = bm._check_exits(pos, 111, 112, 105, False, cfg, row_open=105)
    assert ids(third) == ["tp_1"]


def test_persisted_triggered_exits_survive_restart(bm):
    pos = _pos(triggered=["tp_0"])
    cfg = _settings(take_profits=[_tp("percentage", 5, 50), _tp("percentage", 10, 50)])
    ev = bm._check_exits(pos, 106, 107, 100, False, cfg, row_open=100)
    assert ev == []


def test_trailing_tp_activates_then_trails_previous_peak(bm):
    pos = _pos()
    cfg = _settings(take_profits=[_tp("trailing", 5)])
    # Not yet activated (peak < 105) → nothing, even though price dips
    assert bm._check_exits(pos, 101, 104, 96, False, cfg, row_open=103) == []
    # Rally: peak becomes 120 (activated), no trigger this candle
    assert bm._check_exits(pos, 119, 120, 110, False, cfg, row_open=110) == []
    # Trail = 120 × 0.95 = 114 → low 113 fires
    ev = bm._check_exits(pos, 113.5, 118, 113, False, cfg, row_open=117)
    assert ids(ev) == ["tp_0"]
    assert ev[0]["price"] == pytest.approx(114.0)


def test_stop_loss_wins_over_take_profit_in_same_candle(bm):
    cfg = _settings([_sl("percentage", 10)], [_tp("percentage", 5)])
    ev = bm._check_exits(_pos(), 95, 106, 89, False, cfg, row_open=100)
    assert ids(ev) == ["sl_0"]


# ── strategy sell ──────────────────────────────────────────────────────────

def test_strategy_sell_only_when_no_other_exit_fired(bm):
    cfg = _settings([_sl("percentage", 10)], exit_amount=40)
    ev = bm._check_exits(_pos(), 102, 103, 101, True, cfg, row_open=101)
    assert ids(ev) == ["strategy_sell"]
    assert ev[0]["reason"] == "strategy"
    assert ev[0]["price"] == 102
    assert ev[0]["qty_pct"] == 40
    # SL fired on the same candle → strategy sell is not added on top
    ev2 = bm._check_exits(_pos(pid=2), 89, 95, 88, True, cfg, row_open=95)
    assert ids(ev2) == ["sl_0"]


def test_strategy_sell_defaults_to_full_close(bm):
    ev = bm._check_exits(_pos(), 102, 103, 101, True, _settings(), row_open=101)
    assert ev[0]["qty_pct"] == 100


# ── state bookkeeping ──────────────────────────────────────────────────────

def test_state_is_reset_when_entry_price_changes(bm):
    pos = _pos()
    cfg = _settings([_sl("trailing", 10)])
    bm._check_exits(pos, 140, 150, 130, False, cfg, row_open=130)
    assert bm.position_states[pos.id]["highest_price"] == 150
    # Same id re-used by a new position (e.g. after a cache wipe) → fresh state
    new_pos = _pos(entry=200.0, pid=pos.id)
    bm._check_exits(new_pos, 205, 210, 199, False, cfg, row_open=204)
    assert bm.position_states[pos.id]["entry_price"] == 200.0
    assert bm.position_states[pos.id]["highest_price"] == 210


def test_highest_price_written_back_to_position(bm):
    pos = _pos()
    bm._check_exits(pos, 105, 111, 99, False, _settings(), row_open=100)
    assert pos.highest_price == 111
    bm._check_exits(pos, 104, 108, 99, False, _settings(), row_open=105)
    assert pos.highest_price == 111  # never decreases
