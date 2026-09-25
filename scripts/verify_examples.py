"""Re-run every bundled example strategy on real exchange candles and print
the backtest summary — the numbers documented in STRATEGY_CONTEXT.md §4.9.

Uses a throw-away SQLite file (the real ``data/`` database is never touched)
and the same startup-backtest path the app takes after "Load example" +
Start, so what this prints is what a user sees on the bot card / Analytics.

    apexalgo_venv/bin/python scripts/verify_examples.py            # as shipped
    apexalgo_venv/bin/python scripts/verify_examples.py --one-per-pair
    apexalgo_venv/bin/python scripts/verify_examples.py --only Supertrend

``--one-per-pair`` forces ``max_positions: 1 / per_pair`` (the pre-v2 engine
behaviour) so the run is comparable with the older §4.9 rows. Results drift
slightly day by day because the lookback window slides with the clock.
"""
import argparse
import glob
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

import ccxt
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="apexalgo-verify-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/verify.sqlite3"
os.environ.setdefault("MASTER_API_KEY", "verify-not-secret")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

import backend.main  # noqa: E402,F401  (creates tables + runs migrations)
from backend.core.database import SessionLocal  # noqa: E402
from backend.core.exchange_registry import build_exchange  # noqa: E402
from backend.engine.bot_manager import BotManager  # noqa: E402
from backend.engine.sizing import _tf_seconds  # noqa: E402
from backend.models.bots import BotConfig  # noqa: E402
from backend.models.candles import Candle  # noqa: E402
from backend.models.orders import Order  # noqa: E402
from backend.models.positions import Position  # noqa: E402

_candle_cache: dict[tuple, list] = {}


def fetch_closed_candles(ex: ccxt.Exchange, symbol: str, tf: str, n: int) -> list:
    """The last ``n`` closed candles, paginated forward from ``now - n``."""
    key = (ex.id, symbol, tf, n)
    if key in _candle_cache:
        return _candle_cache[key]
    tf_ms = _tf_seconds(tf) * 1000
    now_ms = int(time.time() * 1000)
    since = now_ms - (n + 5) * tf_ms
    rows: list = []
    while True:
        batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        since = batch[-1][0] + tf_ms
    rows = [r for r in rows if r[0] + tf_ms <= now_ms][-n:]  # drop the forming candle
    _candle_cache[key] = rows
    return rows


def run_example(path: str, one_per_pair: bool) -> None:
    payload = json.load(open(path))
    bot = payload["bot"]
    settings = {**bot["settings"], "backtest_on_start": True, "api_execution": False, "api_key_name": None}
    if one_per_pair:
        settings["max_positions"] = 1
        settings["max_positions_scope"] = "per_pair"
    tf = settings["timeframe"]
    lookback = int(settings["backtest_lookback"])
    exchange_id = settings.get("data_exchange", "binance")
    ex = build_exchange(exchange_id)
    symbols = settings.get("symbols") or [settings["symbol"]]

    db = SessionLocal()
    try:
        for sym in symbols:
            if db.query(Candle).filter(Candle.exchange == exchange_id, Candle.symbol == sym,
                                       Candle.timeframe == tf).count():
                continue  # shared with an earlier example
            rows = fetch_closed_candles(ex, sym, tf, lookback)
            db.bulk_save_objects([
                Candle(exchange=exchange_id, symbol=sym, timeframe=tf,
                       timestamp=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc).replace(tzinfo=None),
                       open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
                for r in rows])
            db.commit()

        name = bot["name"] + (" (1/pair)" if one_per_pair else "")
        cfg = BotConfig(name=name, is_active=True, is_sandbox=True, strategy="node_graph", settings=settings)
        db.add(cfg)
        db.commit()
        BotManager()._execute_sync_backfill(cfg.id)
        db.expire_all()

        sm = db.get(BotConfig, cfg.id).settings.get("last_backtest_summary") or {}
        n_orders = db.query(Order).filter(Order.bot_name == name, Order.mode == "backtest").count()
        n_open = db.query(Position).filter(Position.bot_name == name, Position.mode == "backtest",
                                            Position.status == "open").count()
        entry = settings["trade_settings"]["entry"]
        print(f"\n== {name} | {exchange_id} {tf} | {', '.join(symbols)} | "
              f"max_positions {settings['max_positions']} {settings['max_positions_scope']} | "
              f"size {entry['amount_value']}% | fee {entry['fee']}% slip {entry['slippage']}%")
        print(f"   window  {sm.get('data_from')} -> {sm.get('data_to')}  ({sm.get('candles')} candles, "
              f"{lookback} lookback)")
        ccy = sm.get("cash_currency") or "quote"
        print(f"   trades  {sm.get('trades')} | win {sm.get('win_rate')}% | net {sm.get('net_pnl')} {ccy} | "
              f"return {sm.get('return_pct')}% | max DD {sm.get('max_drawdown')}% | "
              f"blocked {sm.get('entries_blocked_days')} d | orders {n_orders} | open at end {n_open}")
        if sm.get("market_type") == "swap":
            print(f"   swap    {sm.get('leverage')}x | long {sm.get('long_trades')} / short {sm.get('short_trades')} | "
                  f"liquidations {sm.get('liquidations')} | funding {sm.get('funding')} {sm.get('funding_paid')} ({sm.get('funding_events')} settlements) | mmr {sm.get('mmr_source')}")
        bh = sm.get("buy_hold") or {}
        print("   B&H     " + ", ".join(f"{s} {v.get('pct')}%" for s, v in bh.items()))
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--one-per-pair", action="store_true", help="force max_positions 1 / per_pair (pre-v2 behaviour)")
    ap.add_argument("--only", default="", help="substring of the example file name to run")
    args = ap.parse_args()
    paths = sorted(p for p in glob.glob(os.path.join(ROOT, "examples", "*.apex.json")) if args.only in os.path.basename(p))
    if not paths:
        sys.exit("no examples matched")
    print(f"engine run on {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, temp DB {_TMP}")
    for p in paths:
        run_example(p, args.one_per_pair)


if __name__ == "__main__":
    main()
