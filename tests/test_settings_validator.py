"""Validator guardrails that protect the backtest ↔ live contract."""
from backend.engine.settings_validator import validate_bot_settings
from tests.conftest import load_example


def _live(settings, **overrides):
    return {**settings, "api_execution": True, "api_key_name": "k", **overrides}


def test_templates_do_not_warn_about_max_order_value():
    for name in ("Donchian_Breakout_1d", "EMA_Cross_4h", "Supertrend_Trend_1d"):
        _, settings = load_example(name)
        res = validate_bot_settings(_live(settings))
        assert res["errors"] == []
        assert not any("max_order_value" in w for w in res["warnings"]), (name, res["warnings"])


def test_cap_below_planned_entry_warns():
    _, settings = load_example("Donchian_Breakout_1d")  # 50% × 1000 = 500
    res = validate_bot_settings(_live(settings, max_order_value=250))
    assert any("max_order_value (250)" in w and "500" in w for w in res["warnings"])


def test_fixed_amount_is_compared_directly():
    _, settings = load_example("Donchian_Breakout_1d")
    ts = {**settings["trade_settings"], "entry": {**settings["trade_settings"]["entry"], "amount_type": "fixed", "amount_value": 300}}
    res = validate_bot_settings(_live(settings, max_order_value=250, trade_settings=ts))
    assert any("max_order_value" in w for w in res["warnings"])


def test_missing_cap_is_an_error_for_live():
    _, settings = load_example("Donchian_Breakout_1d")
    res = validate_bot_settings(_live(settings, max_order_value=0))
    assert any("max_order_value" in e for e in res["errors"])


def test_zero_fee_warns_even_without_live_execution():
    _, settings = load_example("Donchian_Breakout_1d")
    ts = {**settings["trade_settings"], "entry": {**settings["trade_settings"]["entry"], "fee": 0}}
    res = validate_bot_settings({**settings, "api_execution": False, "trade_settings": ts})
    assert any("without fees" in w for w in res["warnings"])


# ── 1.10 graph hardening ───────────────────────────────────────────────────

def _with_nodes(nodes, entry="entry", **extra):
    _, settings = load_example("Donchian_Breakout_1d")
    return {**settings, "nodes": nodes, "entry_node": entry, "exit_node": None, **extra}


def test_cyclic_graph_is_rejected():
    nodes = {
        "a": {"class": "logic", "operator": "and", "left": "b", "right": "c"},
        "b": {"class": "logic", "operator": "or", "left": "a", "right": "c"},
        "c": {"class": "condition", "left": "close", "operator": ">", "right": 0},
    }
    res = validate_bot_settings(_with_nodes(nodes, entry="a"))
    assert any("cycle" in e and "a -> b -> a" in e for e in res["errors"]), res["errors"]


def test_self_referencing_node_is_a_cycle():
    nodes = {"n": {"class": "condition", "left": "n", "operator": ">", "right": 0}}
    res = validate_bot_settings(_with_nodes(nodes, entry="n"))
    assert any("cycle" in e for e in res["errors"])


def test_streak_length_must_be_a_positive_integer_literal():
    def cond(right):
        return {"c": {"class": "condition", "left": "close", "operator": "increasing_for", "right": right},
                "ema": {"class": "indicator", "method": "ema", "params": {"length": 20}}}
    assert validate_bot_settings(_with_nodes(cond(3), entry="c"))["errors"] == []
    for bad in (0, -2, "ema", "abc"):
        res = validate_bot_settings(_with_nodes(cond(bad), entry="c"))
        assert any("increasing_for" in e for e in res["errors"]), (bad, res["errors"])


def test_reserved_node_ids_are_rejected():
    nodes = {"high": {"class": "price_data", "type": "high", "offset": 1},
             "c": {"class": "condition", "left": "close", "operator": ">", "right": "high"}}
    res = validate_bot_settings(_with_nodes(nodes, entry="c"))
    assert any("reserved" in e and "'high'" in e for e in res["errors"]), res["errors"]


def test_short_lookback_warns_against_longest_indicator():
    nodes = {"ema": {"class": "indicator", "method": "ema", "params": {"length": 200}},
             "c": {"class": "condition", "left": "close", "operator": ">", "right": "ema"}}
    res = validate_bot_settings(_with_nodes(nodes, entry="c", backtest_lookback=220))
    assert any("backtest_lookback (220)" in w and "200" in w for w in res["warnings"]), res["warnings"]
    res = validate_bot_settings(_with_nodes(nodes, entry="c", backtest_lookback=600))
    assert not any("backtest_lookback" in w for w in res["warnings"])


def test_ichimoku_labels_follow_pandas_ta_column_order():
    import numpy as np
    import pandas as pd
    import pandas_ta_classic as ta

    from backend.engine.indicator_registry import get_spec
    n = 120
    df = pd.DataFrame({"high": np.arange(n) + 101.0, "low": np.arange(n) + 99.0, "close": np.arange(n) + 100.5})
    cols = list(ta.ichimoku(df.high, df.low, df.close, tenkan=9, kijun=26, senkou=52)[0].columns)
    prefixes = [c.split("_")[0] for c in cols]
    assert prefixes == ["ISA", "ISB", "ITS", "IKS", "ICS"]
    labels = get_spec("ichimoku").outputs
    assert "Span A" in labels[0] and "Span B" in labels[1] and "Tenkan" in labels[2] and "Kijun" in labels[3] and "Chikou" in labels[4]
