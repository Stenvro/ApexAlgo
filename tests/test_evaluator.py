"""Evaluator guards that must hold even for graphs that bypassed validation
(e.g. an imported bundle edited by hand)."""
import numpy as np
import pandas as pd

from backend.engine.evaluator import NodeEvaluator


def _df(n=30):
    close = 100 + np.arange(n, dtype=float)  # strictly increasing
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0})


def test_cyclic_graph_resolves_to_false_instead_of_recursing():
    ev = NodeEvaluator({"nodes": {
        "a": {"class": "logic", "operator": "and", "left": "b", "right": "c"},
        "b": {"class": "logic", "operator": "or", "left": "a", "right": "c"},
        "c": {"class": "condition", "left": "close", "operator": ">", "right": 0},
    }, "entry_node": "a"})
    ev.df = _df()
    out = ev.resolve_node("a")  # must not raise RecursionError
    # The back-edge b -> a contributes False, so a = (False | c) & c = c
    assert out.equals(ev.resolve_node("c"))


def test_streak_window_is_a_clamped_literal_not_a_series_peek():
    assert NodeEvaluator._streak_length(0) == 1
    assert NodeEvaluator._streak_length(-3) == 1
    assert NodeEvaluator._streak_length("4") == 4
    assert NodeEvaluator._streak_length("some_node") == 2
    assert NodeEvaluator._streak_length(None) == 2

    ev = NodeEvaluator({"nodes": {"c": {"class": "condition", "left": "close", "operator": "increasing_for", "right": 0}}})
    ev.df = _df()
    out = ev.resolve_node("c")  # window 0 would raise in pandas; clamped to 1
    assert bool(out.iloc[-1]) is True and bool(out.iloc[0]) is False
