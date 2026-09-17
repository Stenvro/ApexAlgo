"""Setting coercion, fingerprints and position sizing shared by the backtest
and the live tick. Pure functions over settings + DB — no engine state."""
import json
import logging
from hashlib import md5
from datetime import datetime, timezone
from sqlalchemy import text, func
from backend.models.bots import BotConfig
from backend.models.positions import Position

logger = logging.getLogger("apexalgo.bot_manager")


def _indicator_fingerprint(settings):
    """Stable hash of a bot's indicator node configs."""
    nodes = settings.get("nodes", {})
    ind_nodes = {k: v for k, v in sorted(nodes.items()) if v.get("class") == "indicator"}
    return md5(json.dumps(ind_nodes, sort_keys=True).encode()).hexdigest()


def _num(v, default=0.0):
    """Cast a setting to float, tolerating None and '' (a cleared UI field
    keeps the key present, so dict .get defaults never kick in)."""
    if v is None or v == "":
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _int(v, default=0):
    if v is None or v == "":
        return int(default)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return int(default)


def _tf_seconds(timeframe: str) -> int:
    if timeframe.endswith('m'): return int(timeframe[:-1]) * 60
    if timeframe.endswith('h'): return int(timeframe[:-1]) * 3600
    if timeframe.endswith('d'): return int(timeframe[:-1]) * 86400
    if timeframe.endswith('w'): return int(timeframe[:-1]) * 604800
    return 60


def _naive_utc(ts):
    """SQLite stores naive datetimes; normalize any pandas/tz-aware value to
    naive UTC so unique constraints and dedup lookups compare consistently."""
    if ts is None:
        return None
    if hasattr(ts, 'to_pydatetime'):
        ts = ts.to_pydatetime()
    if getattr(ts, 'tzinfo', None) is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


# Settings that do not change what the strategy does on the data: layout,
# order routing, live sizing and the engine's own runtime bookkeeping.
_NON_STRATEGY_KEYS = frozenset({
    "ui_layout", "api_execution", "api_key_name", "live_allocation_pct", "max_order_value",
    "backtest_on_start", "last_backtest_summary", "last_backtest_max_drawdown",
    "last_stop_reason", "drawdown_peak_reset_at", "live_starting_capital",
})


def _config_fingerprint(settings: dict) -> str:
    """Stable hash of the strategy-relevant part of a bot's settings, used to
    count how many distinct variants have been backtested."""
    relevant = {k: v for k, v in (settings or {}).items() if k not in _NON_STRATEGY_KEYS}
    return md5(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()


def _record_config_run(db, bot_name: str, settings: dict) -> int:
    """Register this configuration as backtested and return the number of
    distinct configurations the bot has run so far (this one included)."""
    db.execute(
        text("INSERT OR IGNORE INTO bot_config_runs (bot_name, config_hash, first_run_at) VALUES (:bn, :h, :ts)"),
        {"bn": bot_name, "h": _config_fingerprint(settings), "ts": datetime.now(timezone.utc).replace(tzinfo=None)},
    )
    return int(db.execute(text("SELECT COUNT(*) FROM bot_config_runs WHERE bot_name = :bn"), {"bn": bot_name}).scalar() or 0)


def sim_frictions(settings):
    """(entry_fee, exit_fee, entry_slippage, exit_slippage) as fractions
    from trade_settings — the frictions the backtest applies to every
    simulated fill. Exit fee falls back to the entry fee when unset."""
    ts = settings.get("trade_settings", {}) or {}
    entry_fee = _num(ts.get("entry", {}).get("fee"), 0) / 100
    raw_exit_fee = ts.get("exit", {}).get("fee")
    exit_fee = _num(raw_exit_fee, entry_fee * 100) / 100 if raw_exit_fee not in (None, "") else entry_fee
    entry_slip = _num(ts.get("entry", {}).get("slippage"), 0) / 100
    exit_slip = _num(ts.get("exit", {}).get("slippage"), 0) / 100
    return entry_fee, exit_fee, entry_slip, exit_slip


def deployed_capital(db, bot_names, quote, modes=("paper", "live")):
    """Quote-currency cost (entry price x amount) of the open positions of
    the given bots in the given modes, in pairs quoted in `quote`."""
    if not bot_names:
        return 0.0
    rows = db.query(Position.entry_price, Position.amount, Position.symbol).filter(
        Position.bot_name.in_(list(bot_names)), Position.status == "open",
        Position.mode.in_(list(modes))).all()
    return sum((r[0] or 0.0) * (r[1] or 0.0) for r in rows if (r[2] or "").replace('-', '/').upper().endswith('/' + quote))


def forward_pool(db, bot, quote):
    """Cash a forward-test bot may still deploy: the backtest's capital
    pool carried forward — backtest_capital plus realized forward-test
    PnL (fees included) minus what its open forward-test positions have
    locked. Same economics as bt_equity, so a forward test sizes exactly
    like the backtest it is meant to confirm."""
    realized = db.query(func.coalesce(func.sum(Position.profit_abs), 0.0)).filter(
        Position.bot_name == bot.name, Position.status == "closed",
        Position.mode == "forward_test").scalar() or 0.0
    deployed = deployed_capital(db, [bot.name], quote, modes=("forward_test",))
    return _num(bot.settings.get("backtest_capital"), 1000) + float(realized) - deployed


def live_allocation(db, bot, quote, free_balance):
    """Capital this bot may still deploy: its share (live_allocation_pct) of
    the wallet's quote equity — free balance plus what every bot on the same
    key already has in open positions — minus its own open positions.
    Returns (pool_remaining, wallet_total, bot_total)."""
    pct = min(max(_num(bot.settings.get("live_allocation_pct"), 100), 0.0), 100.0)
    key_name = bot.settings.get("api_key_name")
    peers = [b.name for b in db.query(BotConfig).all() if (b.settings or {}).get("api_key_name") == key_name]
    if bot.name not in peers:
        peers.append(bot.name)
    deployed_key = deployed_capital(db, peers, quote)
    deployed_bot = deployed_capital(db, [bot.name], quote)
    wallet_total = free_balance + deployed_key
    bot_total = wallet_total * pct / 100.0
    return max(bot_total - deployed_bot, 0.0), wallet_total, bot_total


def calculate_trade_amount(current_price, bot_settings, current_equity=None):
    if not current_price or current_price <= 0:
        logger.warning("Invalid current_price (%s), cannot calculate trade amount", current_price)
        return None

    entry_settings = bot_settings.get("trade_settings", {}).get("entry", {})
    amount_type = entry_settings.get("amount_type", "percentage")
    raw_val = entry_settings.get("amount_value")

    try:
        amount_value = float(raw_val) if raw_val and float(raw_val) > 0 else 100.0
    except (ValueError, TypeError):
        amount_value = 100.0

    if amount_type == "fixed":
        trade_amount = amount_value / current_price
        return max(trade_amount, 0.0001)
    else:
        capital = current_equity if current_equity is not None else _num(bot_settings.get("backtest_capital"), 1000)
        if capital <= 0:
            return None
        investment = capital * (amount_value / 100)
        trade_amount = investment / current_price
        return max(trade_amount, 0.0001)
