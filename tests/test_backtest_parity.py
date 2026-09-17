"""Backtest ↔ live parity rules that the golden templates do not exercise
(their symbol count never exceeds max_positions)."""
import pytest

from backend.engine.bot_manager import BotManager
from backend.models.bots import BotConfig
from backend.models.positions import Position
from tests.conftest import insert_candles, make_candles

EXCHANGE, TF = "binance", "1h"
SYMBOLS = ("BTC/USDT", "ETH/USDT")
N_CANDLES = 400


def _settings(max_positions, scope):
    return {
        "symbols": list(SYMBOLS), "timeframe": TF, "data_exchange": EXCHANGE,
        "api_execution": False, "api_key_name": None,
        "backtest_on_start": True, "backtest_lookback": 300, "backtest_capital": 1000,
        "max_positions": max_positions, "max_positions_scope": scope,
        "trade_settings": {
            "entry": {"order_type": "market", "amount_type": "percentage", "amount_value": 40,
                      "fee": 0.1, "slippage": 0,
                      "stop_losses": [{"type": "percentage", "value": 3, "close_amount_type": "percentage", "close_amount_value": 100}],
                      "take_profits": [{"type": "percentage", "value": 3, "close_amount_type": "percentage", "close_amount_value": 100}]},
            "exit": {"order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.1, "slippage": 0},
        },
        "nodes": {"entry": {"class": "condition", "left": "close", "operator": ">", "right": 0}},
        "entry_node": "entry",
        "exit_node": None,
    }


def _run(db, settings):
    for i, sym in enumerate(SYMBOLS):
        insert_candles(db, make_candles(EXCHANGE, sym, TF, N_CANDLES, seed=11 + i, start_price=100.0 * (i + 1)))
    bot = BotConfig(name="bt-bot", is_active=True, is_sandbox=True, strategy="node_graph", settings=settings)
    db.add(bot)
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    return db.query(Position).filter(Position.bot_name == "bt-bot", Position.mode == "backtest").order_by(Position.created_at).all()


def _max_concurrent(positions):
    events = [(p.created_at, 1) for p in positions] + [(p.closed_at, -1) for p in positions if p.closed_at]
    events.sort(key=lambda e: (e[0], e[1]))  # closes before opens at the same timestamp
    peak = cur = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def test_global_max_positions_caps_the_backtest_portfolio(db):
    positions = _run(db, _settings(max_positions=1, scope="global"))
    assert len(positions) > 2
    assert {p.symbol for p in positions} == set(SYMBOLS), "both symbols must get their turn"
    assert _max_concurrent(positions) == 1


def test_per_pair_scope_still_allows_one_position_per_symbol(db):
    positions = _run(db, _settings(max_positions=1, scope="per_pair"))
    assert _max_concurrent(positions) == 2


def test_per_pair_pyramids_up_to_max_positions_on_each_symbol(db):
    """entry is always true, so every candle adds a position until the cap"""
    positions = _run(db, _settings(max_positions=3, scope="per_pair"))
    for sym in SYMBOLS:
        assert _max_concurrent([p for p in positions if p.symbol == sym]) == 3
    assert _max_concurrent(positions) == 6


def test_global_scope_pyramids_up_to_max_positions_across_the_portfolio(db):
    positions = _run(db, _settings(max_positions=3, scope="global"))
    assert _max_concurrent(positions) == 3
    assert {p.symbol for p in positions} == set(SYMBOLS)


def test_each_pyramided_position_has_its_own_stops_and_a_sell_flattens_the_pair(db):
    from backend.models.orders import Order
    s = _settings(max_positions=3, scope="per_pair")
    # Wide stops so the SELL signal (red candle) is what closes the layers
    s["trade_settings"]["entry"]["stop_losses"][0]["value"] = 90
    s["trade_settings"]["entry"]["take_profits"][0]["value"] = 900
    s["nodes"]["exit"] = {"class": "condition", "left": "close", "operator": "<", "right": "open"}
    s["exit_node"] = "exit"
    positions = _run(db, s)
    assert _max_concurrent(positions) == 6
    for p in positions:
        orders = db.query(Order).filter(Order.position_id == p.id, Order.status == "filled").all()
        # Every layer enters once and exits once, on its own candle
        assert sorted(o.side for o in orders) == ["buy", "sell"]
        assert p.status == "closed"
    # A SELL closes every layer of the pair that was open before that candle
    # (the layer opened on the SELL candle itself waits for the next one)
    for sym in SYMBOLS:
        sym_pos = [p for p in positions if p.symbol == sym]
        last_ts = max(p.closed_at for p in sym_pos)  # end-of-data flatten, not a SELL
        for ts in {p.closed_at for p in sym_pos if p.closed_at != last_ts}:
            open_before = {p.id for p in sym_pos if p.created_at < ts and (p.closed_at >= ts)}
            closed_at_ts = {p.id for p in sym_pos if p.closed_at == ts}
            assert open_before == closed_at_ts, f"{sym} @ {ts}: partial flatten"


def test_backtest_partial_take_profits_are_a_share_of_the_original_amount(db):
    from backend.models.orders import Order
    s = _settings(max_positions=1, scope="per_pair")
    s["trade_settings"]["entry"]["take_profits"] = [
        {"type": "percentage", "value": 1, "close_amount_type": "percentage", "close_amount_value": 50},
        {"type": "percentage", "value": 2, "close_amount_type": "percentage", "close_amount_value": 50},
    ]
    positions = _run(db, s)
    two_step = 0
    for p in positions:
        orders = db.query(Order).filter(Order.position_id == p.id, Order.status == "filled").order_by(Order.id).all()
        buys = [o.amount for o in orders if o.side == "buy"]
        sells = [o.amount for o in orders if o.side == "sell"]
        assert len(buys) == 1
        assert sum(sells) == pytest.approx(buys[0], rel=1e-9)
        if len(sells) == 2:
            two_step += 1
            assert sells[0] == pytest.approx(buys[0] * 0.5, rel=1e-9)
            assert sells[1] == pytest.approx(buys[0] * 0.5, rel=1e-9)
    assert two_step > 0, "synthetic data must produce at least one two-step exit"
