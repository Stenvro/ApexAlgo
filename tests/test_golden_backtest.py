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
    return _run_settings(db, bot_name, settings)


def _run_settings(db, bot_name, settings, vol=0.02):
    settings = {**settings, "backtest_lookback": LOOKBACK, "backtest_on_start": True,
                "api_execution": False, "api_key_name": None}
    exchange = settings.get("data_exchange", "okx")
    timeframe = settings["timeframe"]
    symbols = settings.get("symbols") or [settings["symbol"]]
    for i, symbol in enumerate(symbols):
        insert_candles(db, make_candles(exchange, symbol, timeframe, CANDLES_PER_SYMBOL,
                                        seed=1000 + i, start_price=100.0 * (i + 1), vol=vol))

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
        # `cash_currency` (sprint D) is asserted explicitly per test instead of
        # being pinned, so the pre-existing goldens stay byte-identical
        "summary": {k: v for k, v in (db.get(BotConfig, bot.id).settings.get("last_backtest_summary") or {}).items()
                    if k not in ("finished_at", "cash_currency")},
        "cash_currency": (db.get(BotConfig, bot.id).settings.get("last_backtest_summary") or {}).get("cash_currency"),
    }
    return snapshot


def _compare_with_golden(snapshot, name):
    assert snapshot["orders"], "a golden run with zero trades pins nothing — adjust the synthetic data"
    assert snapshot["open_positions_after_run"] == 0, "backtest must end flat"

    golden_path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1" or not golden_path.exists():
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden_path.write_text(json.dumps({k: v for k, v in snapshot.items() if k != "cash_currency"}, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"golden snapshot written: {golden_path.relative_to(GOLDEN_DIR.parents[1])}")

    expected = json.loads(golden_path.read_text())
    assert snapshot["summary"] == expected["summary"]
    # every golden trades one quote-settled unit: the summary must name it
    assert snapshot["cash_currency"] == snapshot["orders"][0]["symbol"].split("/")[1].split(":")[0]
    assert len(snapshot["orders"]) == len(expected["orders"])
    for got, want in zip(snapshot["orders"], expected["orders"]):
        assert got == want


@pytest.mark.parametrize("name", example_names())
def test_example_matches_golden(db, name):
    _compare_with_golden(_run_example(db, name), name)


# ── perpetual swap: long + short on one EMA cross ─────────────────────────
#
# Not a bundled example (the examples are the vetted spot templates): a
# synthetic 5x swap strategy that is long while the fast EMA is above the
# slow one and short while it is below, with a percentage stop on both legs
# and no take profit (the 30% stop sits beyond the 5x liquidation level), so the
# order stream pins margin sizing, short PnL, reduce-only covers and
# liquidations of the derivatives path.
SWAP_GOLDEN = "Synthetic_Swap_LongShort_4h"
SWAP_SETTINGS = {
    "symbol": "BTC/USDT:USDT", "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"], "timeframe": "4h",
    "data_exchange": "okx", "market_type": "swap", "leverage": 5, "margin_mode": "isolated",
    "max_positions": 2, "max_positions_scope": "global", "cooldown_trades": 1, "cooldown_candles": 3,
    "max_drawdown": 80, "drawdown_action": "block_entries", "drawdown_cooldown_days": 7,
    "max_capital_loss": 90, "max_order_value": 3000, "live_allocation_pct": 100,
    "backtest_capital": 1000,
    "trade_settings": {
        "entry": {"order_type": "market", "amount_type": "percentage", "amount_value": 40, "fee": 0.05, "slippage": 0.05,
                  "take_profits": [], "stop_losses": [{"type": "percentage", "value": 30, "close_amount_type": "percentage", "close_amount_value": 100}]},
        "exit": {"order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.05, "slippage": 0.05},
        "short": {"order_type": "market", "amount_type": "percentage", "amount_value": 40, "fee": 0.05, "slippage": 0.05,
                  "take_profits": [], "stop_losses": [{"type": "percentage", "value": 30, "close_amount_type": "percentage", "close_amount_value": 100}]},
        "cover": {"order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.05, "slippage": 0.05},
    },
    "nodes": {
        "ema_fast": {"class": "indicator", "method": "ema", "params": {"length": 9}, "output_idx": 0},
        "ema_slow": {"class": "indicator", "method": "ema", "params": {"length": 30}, "output_idx": 0},
        "uptrend": {"class": "condition", "left": "ema_fast", "operator": ">", "right": "ema_slow"},
        "downtrend": {"class": "condition", "left": "ema_fast", "operator": "<", "right": "ema_slow"},
        "golden_cross": {"class": "condition", "left": "ema_fast", "operator": "cross_above", "right": "ema_slow"},
        "death_cross": {"class": "condition", "left": "ema_fast", "operator": "cross_below", "right": "ema_slow"},
    },
    # Entries are trend *states* (not the cross) because a long and a short
    # never coexist on a pair: on the cross candle the opposite entry is
    # ignored while the old side is still open, so the new side opens on
    # the next candle from the state signal, throttled by the cooldown.
    "entry_node": "uptrend", "exit_node": "death_cross",
    "short_node": "downtrend", "cover_node": "golden_cross",
}


def test_swap_long_short_matches_golden(db):
    snapshot = _run_settings(db, "swap-golden", SWAP_SETTINGS, vol=0.05)
    summary = snapshot["summary"]
    # the run must actually exercise the derivatives path before it pins anything
    assert summary["market_type"] == "swap" and summary["leverage"] == 5
    assert summary["long_trades"] > 0 and summary["short_trades"] > 0
    assert summary["liquidations"] > 0
    assert any(o["side"] == "sell" for o in snapshot["orders"]) and any(o["side"] == "buy" for o in snapshot["orders"])
    _compare_with_golden(snapshot, SWAP_GOLDEN)

