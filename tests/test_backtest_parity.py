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


@pytest.mark.parametrize("scope", ["global", "per_pair"])
def test_never_more_than_one_position_per_symbol(db, scope):
    positions = _run(db, _settings(max_positions=5, scope=scope))
    for sym in SYMBOLS:
        assert _max_concurrent([p for p in positions if p.symbol == sym]) == 1


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
