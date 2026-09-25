"""Setting coercion, fingerprints and position sizing shared by the backtest
and the live tick. Pure functions over settings + DB — no engine state."""
import json
import logging
from hashlib import md5, sha256
from datetime import datetime, timezone
from sqlalchemy import text, func
from backend.models.bots import BotConfig
from backend.models.positions import Position
from backend.models.orders import Order
from backend.engine import pnl
from backend.engine.contracts import ContractSpec, spec_for, spec_from_symbol
from backend.engine.symbols import cash_currency

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
# order routing, live sizing, the pinned backtest window (which data, not
# what the strategy does on it) and the engine's own runtime bookkeeping.
# `max_order_value` is *not* here: it caps the backtest/forward sizing too,
# so it changes the trades and counts as a variant.
_NON_STRATEGY_KEYS = frozenset({
    "ui_layout", "api_execution", "api_key_name", "live_allocation_pct",
    "backtest_on_start", "backtest_from", "backtest_to",
    "last_backtest_summary", "last_backtest_max_drawdown",
    "last_stop_reason", "drawdown_peak_reset_at", "live_starting_capital",
})


# Phase-2 (derivatives) settings at their default are dropped from the
# fingerprint: a spot bot saved by a newer UI must hash exactly like the
# same bot saved before these keys existed, or every variant counter bumps.
_DEFAULT_MARKET_KEYS = {"market_type": "spot", "leverage": 1, "margin_mode": "isolated",
                        # phase 3: absent short/cover nodes hash like a pre-shorts bot
                        "short_node": None, "cover_node": None}


def _config_fingerprint(settings: dict) -> str:
    """Stable hash of the strategy-relevant part of a bot's settings, used to
    count how many distinct variants have been backtested."""
    relevant = {k: v for k, v in (settings or {}).items() if k not in _NON_STRATEGY_KEYS}
    for k, default in _DEFAULT_MARKET_KEYS.items():
        if k in relevant and _is_default_market_setting(relevant[k], default):
            del relevant[k]
    return md5(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()


def _is_default_market_setting(value, default) -> bool:
    if value in (None, ""):
        return True
    if isinstance(default, (int, float)):
        try:
            return float(value) == float(default)
        except (TypeError, ValueError):
            return False
    return str(value).lower() == default


def data_fingerprint(df) -> str:
    """sha256 of the raw OHLCV rows a backtest walked (sorted by timestamp).
    `repr` on the floats makes it byte-stable across runs, so two runs on the
    same slice hash equal unless a candle was actually altered — the check
    behind the "historical data changed" warning."""
    h = sha256()
    if df is None or len(df) == 0:
        return h.hexdigest()
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    for ts, o, hi, lo, c, v in df.sort_values("timestamp")[cols].itertuples(index=False, name=None):
        ts = _naive_utc(ts)
        h.update(f"{ts.isoformat() if ts is not None else ''},{o!r},{hi!r},{lo!r},{c!r},{v!r}\n".encode())
    return h.hexdigest()


def backtest_pin(settings) -> tuple:
    """``(window_from, window_to)`` as naive UTC datetimes when the bot's
    backtest window is pinned (``backtest_from``/``backtest_to`` ISO strings),
    else ``(None, None)``. A half or malformed pin counts as no pin."""
    out = []
    for key in ("backtest_from", "backtest_to"):
        raw = (settings or {}).get(key)
        if not raw:
            return None, None
        try:
            out.append(_naive_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))))
        except (TypeError, ValueError):
            return None, None
    if out[0] >= out[1]:
        return None, None
    return out[0], out[1]


def combined_fingerprint(by_symbol: dict) -> str:
    """One hash over the per-symbol data hashes (sorted by symbol)."""
    return sha256("|".join(f"{s}:{by_symbol[s]}" for s in sorted(by_symbol)).encode()).hexdigest()


def slice_key(exchange: str, symbols, timeframe: str, window_from, window_to) -> str:
    """Identity of a backtest slice: which candles were walked. Same pairs,
    timeframe and window on the same exchange = same slice, whatever the
    strategy settings are."""
    f = _naive_utc(window_from)
    t = _naive_utc(window_to)
    parts = [str(exchange or ""), ",".join(sorted(str(s) for s in (symbols or []))), str(timeframe or ""),
             f.isoformat() if f else "", t.isoformat() if t else ""]
    return md5("|".join(parts).encode()).hexdigest()


def _record_config_run(db, bot_name: str, settings: dict, slice_hash: str = "", data_hash: str = "",
                       window_from=None, window_to=None) -> tuple:
    """Log this backtest run and return ``(variants_total, variants_on_slice)``:
    distinct configurations the bot has ever run, and distinct configurations
    run on exactly this slice of data (this one included). One row per
    (config, slice, data) — a rerun on unchanged data is a no-op."""
    db.execute(
        text("INSERT OR IGNORE INTO bot_config_runs (bot_name, config_hash, slice_key, window_from, window_to, data_hash, run_at) "
             "VALUES (:bn, :h, :sk, :wf, :wt, :dh, :ts)"),
        {"bn": bot_name, "h": _config_fingerprint(settings), "sk": slice_hash or "", "wf": _naive_utc(window_from),
         "wt": _naive_utc(window_to), "dh": data_hash or "", "ts": datetime.now(timezone.utc).replace(tzinfo=None)},
    )
    total = int(db.execute(text("SELECT COUNT(DISTINCT config_hash) FROM bot_config_runs WHERE bot_name = :bn"),
                           {"bn": bot_name}).scalar() or 0)
    on_slice = int(db.execute(text("SELECT COUNT(DISTINCT config_hash) FROM bot_config_runs WHERE bot_name = :bn AND slice_key = :sk"),
                              {"bn": bot_name, "sk": slice_hash or ""}).scalar() or 0)
    return total, on_slice


def sim_frictions(settings, side="long"):
    """(entry_fee, exit_fee, entry_slippage, exit_slippage) as fractions
    from trade_settings — the frictions the backtest applies to every
    simulated fill. Exit fee falls back to the entry fee when unset. For a
    short the `short`/`cover` legs are read (falling back to entry/exit)."""
    ts = settings.get("trade_settings", {}) or {}
    open_cfg = pnl.entry_cfg(ts, side)
    close_cfg = pnl.exit_cfg(ts, side)
    entry_fee = _num(open_cfg.get("fee"), 0) / 100
    raw_exit_fee = close_cfg.get("fee")
    exit_fee = _num(raw_exit_fee, entry_fee * 100) / 100 if raw_exit_fee not in (None, "") else entry_fee
    entry_slip = _num(open_cfg.get("slippage"), 0) / 100
    exit_slip = _num(close_cfg.get("slippage"), 0) / 100
    return entry_fee, exit_fee, entry_slip, exit_slip


def position_spec(symbol, contract_kind=None, contract_size=None, exchange=None):
    """`ContractSpec` for a stored position: the kind/size it was booked
    with (so a later contract-size change on the exchange never rewrites
    history), else the registry's cached market, else the symbol's shape."""
    if contract_kind:
        shape = spec_from_symbol(symbol, contract_size)
        return ContractSpec(symbol=shape.symbol, kind=str(contract_kind), base=shape.base, quote=shape.quote,
                            settle=shape.settle, contract_size=shape.contract_size)
    return spec_for(exchange, symbol)


def deployed_capital(db, bot_names, quote, modes=("paper", "live")):
    """Cash locked by the open positions of the given bots in the given
    modes, in pairs funded in `quote` (the cash currency): entry price x
    amount on spot, the margin (notional / leverage) on derivatives."""
    if not bot_names:
        return 0.0
    rows = db.query(Position.entry_price, Position.amount, Position.symbol, Position.leverage,
                    Position.contract_kind, Position.contract_size, Position.exchange).filter(
        Position.bot_name.in_(list(bot_names)), Position.status == "open",
        Position.mode.in_(list(modes))).all()
    total = 0.0
    for entry, amount, sym, lev, kind, size, exchange in rows:
        spec = position_spec(sym, kind, size, exchange)
        if spec.cash_currency != quote:
            continue
        total += spec.margin(amount, entry, lev if (lev and lev > 1) else 1)
    return total


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
    # The entry fee left the pool on open as well (bt_equity -= cost + fee)
    open_ids = [pid for (pid, sym) in db.query(Position.id, Position.symbol).filter(
        Position.bot_name == bot.name, Position.status == "open",
        Position.mode == "forward_test").all() if cash_currency(sym) == quote]
    entry_fees = 0.0
    if open_ids:
        entry_fees = db.query(func.coalesce(func.sum(Order.fee), 0.0)).filter(
            Order.position_id.in_(open_ids), Order.mode == "forward_test",
            func.coalesce(Order.reduce_only, 0) == 0).scalar() or 0.0
    return _num(bot.settings.get("backtest_capital"), 1000) + float(realized) - deployed - float(entry_fees)


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


def max_affordable_amount(equity, entry_price, leverage=1, fee=0.0, spec=None):
    """Largest size whose locked capital (margin plus entry fee on the
    notional) fits in `equity` at `entry_price`. Used to clamp percentage
    sizing so slippage + fee never push an entry past the pool; reduces to
    `equity / (price * (1 + fee))` on spot. Contracts on an inverse `spec`."""
    if spec is not None and spec.is_inverse:
        return spec.max_affordable_qty(equity, entry_price, leverage, fee)
    lev = max(float(leverage or 1), 1.0)
    return equity / (entry_price * (1 / lev + float(fee or 0.0)))


def cap_by_max_order_value(trade_amount, price, settings, spec=None):
    """Backtest/forward/live mirror of the `max_order_value` clamp: the
    *quote* notional of one entry (USD on ``BTC/USD:BTC``, EUR on
    ``BTC/EUR``) never exceeds the cap. `max_order_value` is defined in the
    pair's quote currency on every market kind."""
    cap = _num(settings.get("max_order_value"), 0)
    if trade_amount is None or cap <= 0 or not price or price <= 0:
        return trade_amount
    if spec is not None and spec.is_inverse:
        if spec.notional_quote(trade_amount, price) > cap:
            return spec.qty_for_quote_notional(cap, price)
        return trade_amount
    if trade_amount * price > cap:
        return cap / price
    return trade_amount


def calculate_trade_amount(current_price, bot_settings, current_equity=None, leverage=1, side="long", spec=None):
    """Position size to buy (or sell short): base amount on spot/linear,
    contracts on an inverse `spec`. `amount_value` is what the bot puts up
    (in its cash currency): a `percentage` of the *free cash* in its pool
    (`current_equity` = cash not locked in open positions, not the
    mark-to-market equity — so a second layer at 50% is 50% of what is left,
    not of the account), or a `fixed` cash amount; on derivatives that is the
    margin, so the notional — and the returned size — is `leverage` times
    bigger. Spot callers never pass leverage and get the pre-existing sizing;
    a short reads `trade_settings.short` (fallback `entry`)."""
    if not current_price or current_price <= 0:
        logger.warning("Invalid current_price (%s), cannot calculate trade amount", current_price)
        return None
    try:
        leverage = float(leverage or 1)
    except (TypeError, ValueError):
        leverage = 1.0
    if leverage < 1:
        leverage = 1.0

    entry_settings = pnl.entry_cfg(bot_settings.get("trade_settings", {}), side)
    amount_type = entry_settings.get("amount_type", "percentage")
    raw_val = entry_settings.get("amount_value")

    try:
        amount_value = float(raw_val) if raw_val and float(raw_val) > 0 else 100.0
    except (ValueError, TypeError):
        amount_value = 100.0

    if amount_type == "fixed":
        investment = amount_value
    else:
        capital = current_equity if current_equity is not None else _num(bot_settings.get("backtest_capital"), 1000)
        if capital <= 0:
            return None
        investment = capital * (amount_value / 100)
    if spec is not None and spec.is_inverse:
        trade_amount = spec.qty_for_cash(investment, current_price, leverage)
    else:
        trade_amount = investment / current_price
        if leverage != 1.0:
            trade_amount *= leverage
    return max(trade_amount, 0.0001)
