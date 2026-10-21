"""Drawdown / capital-loss bookkeeping for the live risk gates. The
tracker keeps one realized-equity state per (bot, mode group), lazily rebuilt
from closed positions; `unrealized_pnl` marks the open positions to the
latest stored close so the gates measure the same curve as the backtest."""
import threading
from datetime import datetime, timezone
from sqlalchemy import func
from backend.engine import pnl
from backend.engine.sizing import _naive_utc, currency_filter, position_spec
from backend.models.candles import Candle
from backend.models.positions import Position


MODES_BY_GROUP = {
    "backtest": ("backtest",),
    "forward": ("forward_test",),
    "live": ("paper", "live"),
}


def mode_group_for(mode):
    """Drawdown group a position/bot mode belongs to."""
    for group, modes in MODES_BY_GROUP.items():
        if mode in modes:
            return group
    return "live"


class DrawdownTracker:
    def __init__(self):
        self.cache = {}  # (bot_name, mode_group) -> {starting_capital, peak_equity, running_pnl, max_dd}
        # Guards read-modify-write on the drawdown state: the exit loop, the
        # router's force-close and delete paths all touch it from their own threads
        self.lock = threading.Lock()

    def get(self, bot_name, db, mode_group="live", starting_capital=1000.0, peak_reset_at=None, cash_currency=None):
        """Return cached drawdown state, lazy-initializing from DB on first access.
        mode_group: "backtest" for backtest-only, "forward" for forward_test,
        "live" for paper/live (see `mode_group_for`) — a simulated forward
        loss never counts against the real-money curve.
        starting_capital: wallet size used as equity base for percentage calculation.
        peak_reset_at: naive-UTC datetime; closes before it only move the equity
        base (block_entries cooldown started a new drawdown campaign there).
        cash_currency: when given, only positions booked in that currency
        build the curve (`starting_capital` is in it) — closes of a pair the
        bot traded in another currency are not added to it."""
        cache_key = (bot_name, mode_group)
        with self.lock:
            if cache_key in self.cache:
                return self.cache[cache_key]
        modes = MODES_BY_GROUP.get(mode_group, MODES_BY_GROUP["live"])
        query = db.query(Position.profit_abs, Position.closed_at).filter(
            Position.bot_name == bot_name, Position.status == "closed", Position.mode.in_(modes)
        )
        if cash_currency:
            query = query.filter(currency_filter(cash_currency))
        closed = query.order_by(Position.closed_at).all()
        # Partial exits book their PnL on the still-open position as they
        # fill (and `update` the state per leg), so the realized curve
        # includes them before the last leg closes
        open_q = db.query(func.coalesce(func.sum(Position.profit_abs), 0.0)).filter(
            Position.bot_name == bot_name, Position.status == "open", Position.mode.in_(modes))
        if cash_currency:
            open_q = open_q.filter(currency_filter(cash_currency))
        open_realized = open_q.scalar() or 0.0
        running = 0.0
        peak_equity = starting_capital
        dd = 0.0
        for cp in closed:
            running += (cp.profit_abs or 0)
            equity = starting_capital + running
            if peak_reset_at is not None and cp.closed_at is not None and cp.closed_at < peak_reset_at:
                peak_equity = equity  # pre-reset history: base only, no drawdown
                continue
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                dd = max(dd, ((peak_equity - equity) / peak_equity) * 100)
        running += float(open_realized)
        peak_equity = max(peak_equity, starting_capital + running)
        with self.lock:
            self.cache.setdefault(cache_key, {"starting_capital": starting_capital, "peak_equity": peak_equity, "running_pnl": running, "max_dd": dd})
            return self.cache[cache_key]

    def update(self, bot_name, mode_group, profit_abs):
        """Incrementally update the drawdown state with the realized PnL of
        one closed leg (partial or final)."""
        cache_key = (bot_name, mode_group)
        with self.lock:
            s = self.cache.get(cache_key)
            if s is None:
                return
            s["running_pnl"] += (profit_abs or 0)
            equity = s["starting_capital"] + s["running_pnl"]
            s["peak_equity"] = max(s["peak_equity"], equity)
            if s["peak_equity"] > 0:
                s["max_dd"] = max(s["max_dd"], ((s["peak_equity"] - equity) / s["peak_equity"]) * 100)

    def now(self, state, unrealized=0.0):
        """Current (not historical-max) drawdown % and capital-loss % from a
        drawdown-cache state, marked to market: `unrealized` is the open PnL
        of the bot's open positions at the latest closes. The peak and max
        drawdown are advanced on the mark-to-market curve too — the same
        quantity the backtest gate measures, so a limit that held in the
        backtest means the same thing live. Current drawdown recovers as
        equity climbs back, which is what the block_entries hysteresis needs."""
        with self.lock:
            equity = state["starting_capital"] + state["running_pnl"] + unrealized
            state["peak_equity"] = max(state["peak_equity"], equity)
            peak = state["peak_equity"]
            dd = ((peak - equity) / peak) * 100 if peak > 0 else 0.0
            state["max_dd"] = max(state["max_dd"], dd)
            start = state["starting_capital"]
            loss = ((start - equity) / start) * 100 if start > 0 else 0.0
        return max(dd, 0.0), max(loss, 0.0)


# ── entry-block persistence (block_entries) ────────────────────────────────
# The in-memory `engine._entries_blocked` set is lost on a restart; the block
# is recorded in the bot's settings so the first tick after a restart
# re-arms it instead of resuming entries while the drawdown is still above
# the resume threshold. Cleared when the block lifts (and by a cache wipe).
BLOCK_KEY = "entries_blocked_at"
BLOCK_REASON_KEY = "entries_blocked_reason"


def persist_block(db, bot, reason=None, at=None):
    """Record that the drawdown/capital-loss entry block engaged for `bot`
    (settings `entries_blocked_at` / `entries_blocked_reason`). Reassigns
    `bot.settings` (JSON column) and flushes; the caller's commit persists it."""
    at = _naive_utc(at) if at is not None else _naive_utc(datetime.now(timezone.utc))
    settings = {**(bot.settings or {}), BLOCK_KEY: at.isoformat()}
    if reason:
        settings[BLOCK_REASON_KEY] = str(reason)
    else:
        settings.pop(BLOCK_REASON_KEY, None)
    bot.settings = settings
    db.flush()


def clear_block(db, bot):
    """Forget a persisted entry block (the block lifted or the bot stopped)."""
    if not bot.settings or BLOCK_KEY not in bot.settings and BLOCK_REASON_KEY not in bot.settings:
        return
    bot.settings = {k: v for k, v in bot.settings.items() if k not in (BLOCK_KEY, BLOCK_REASON_KEY)}
    db.flush()


def restore_blocked(bot):
    """Whether a persisted entry block is on record for `bot` (returns the
    block timestamp as naive UTC, else None) — the startup re-arms the
    in-memory block from it; `drawdown_action: block_entries` only."""
    raw = (bot.settings or {}).get(BLOCK_KEY)
    if not raw or (bot.settings or {}).get("drawdown_action", "close_all") != "block_entries":
        return None
    try:
        return _naive_utc(datetime.fromisoformat(str(raw)))
    except (ValueError, TypeError):
        return None


def unrealized_pnl(db, positions, exchange, timeframe, close_cache, candle_ts=None):
    """Open PnL of `positions` at each symbol's stored close at or before
    `candle_ts` (the latest close when None) — one Candle query per symbol
    per tick, memoized in `close_cache`. Windowing at `candle_ts` keeps a
    backlog replay from marking old positions to today's price."""
    total = 0.0
    for p in positions:
        key = (p.exchange or exchange, p.symbol)
        if key not in close_cache:
            q = db.query(Candle.close).filter(
                Candle.exchange == key[0], Candle.symbol == p.symbol, Candle.timeframe == timeframe
            )
            if candle_ts is not None:
                q = q.filter(Candle.timestamp <= candle_ts)
            row = q.order_by(Candle.timestamp.desc()).first()
            close_cache[key] = float(row[0]) if row and row[0] is not None else None
        last = close_cache[key]
        if last is None:
            continue
        spec = position_spec(p.symbol, p.contract_kind, p.contract_size, p.exchange or exchange)
        total += pnl.price_pnl(p.side, p.entry_price or 0.0, last, p.amount or 0.0, spec=spec)
    return total
