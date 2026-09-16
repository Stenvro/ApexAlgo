"""Golden snapshot of the backtest engine.

Every bundled example strategy is run over deterministic synthetic candles
and the resulting order stream + ``last_backtest_summary`` are compared with
``tests/golden/<example>.json``. A diff here means the simulation changed:
either a regression, or an intentional behaviour change — in the latter case
regenerate with ``UPDATE_GOLDEN=1 pytest tests/test_golden_backtest.py`` and
review the diff before committing.
"""
import json
import os
from pathlib import Path

import pytest

from backend.engine.bot_manager import BotManager
from backend.models.bots import BotConfig
from backend.models.orders import Order
from backend.models.positions import Position
from tests.conftest import example_names, insert_candles, load_example, make_candles

GOLDEN_DIR = Path(__file__).parent / "golden"
CANDLES_PER_SYMBOL = 700
LOOKBACK = 600  # smaller than the templates' own lookback so no fetch/wait path is entered


def _run_example(db, name):
    bot_name, settings = load_example(name)
    settings = {**settings, "backtest_lookback": LOOKBACK, "backtest_on_start": True,
                "api_execution": False, "api_key_name": None}
    exchange = settings.get("data_exchange", "okx")
    timeframe = settings["timeframe"]
    symbols = settings.get("symbols") or [settings["symbol"]]
    for i, symbol in enumerate(symbols):
        insert_candles(db, make_candles(exchange, symbol, timeframe, CANDLES_PER_SYMBOL,
                                        seed=1000 + i, start_price=100.0 * (i + 1)))

    bot = BotConfig(name=bot_name, is_active=True, is_sandbox=True, strategy="node_graph", settings=settings)
    db.add(bot)
    db.commit()

    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()

    orders = db.query(Order).filter(Order.bot_name == bot_name, Order.mode == "backtest") \
        .order_by(Order.timestamp, Order.symbol, Order.side, Order.id).all()
    snapshot = {
        "orders": [
            {
                "timestamp": o.timestamp.isoformat(), "symbol": o.symbol, "side": o.side,
                "price": round(o.price, 8), "amount": round(o.amount, 8), "fee": round(o.fee or 0.0, 8),
            }
            for o in orders
        ],
        "open_positions_after_run": db.query(Position).filter(
            Position.bot_name == bot_name, Position.mode == "backtest", Position.status == "open").count(),
        "summary": {k: v for k, v in (db.get(BotConfig, bot.id).settings.get("last_backtest_summary") or {}).items()
                    if k != "finished_at"},
    }
    return snapshot


@pytest.mark.parametrize("name", example_names())
def test_example_matches_golden(db, name):
    snapshot = _run_example(db, name)
    assert snapshot["orders"], "a golden run with zero trades pins nothing — adjust the synthetic data"
    assert snapshot["open_positions_after_run"] == 0, "backtest must end flat"

    golden_path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1" or not golden_path.exists():
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden_path.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"golden snapshot written: {golden_path.relative_to(GOLDEN_DIR.parents[1])}")

    expected = json.loads(golden_path.read_text())
    assert snapshot["summary"] == expected["summary"]
    assert len(snapshot["orders"]) == len(expected["orders"])
    for got, want in zip(snapshot["orders"], expected["orders"]):
        assert got == want
