"""Drawdown / capital-loss bookkeeping for the live risk gates. The
tracker keeps one realized-equity state per (bot, mode group), lazily rebuilt
from closed positions; `unrealized_pnl` marks the open positions to the
latest stored close so the gates measure the same curve as the backtest."""
import threading
from backend.models.candles import Candle
from backend.models.positions import Position


class DrawdownTracker:
    def __init__(self):
        self.cache = {}  # (bot_name, mode_group) -> {starting_capital, peak_equity, running_pnl, max_dd}
        # Guards read-modify-write on the drawdown state: the exit loop, the
        # router's force-close and delete paths all touch it from their own threads
        self.lock = threading.Lock()

    def get(self, bot_name, db, mode_group="live", starting_capital=1000.0, peak_reset_at=None):
        """Return cached drawdown state, lazy-initializing from DB on first access.
        mode_group: "backtest" for backtest-only, "live" for forward_test/paper/live.
        starting_capital: wallet size used as equity base for percentage calculation.
        peak_reset_at: naive-UTC datetime; closes before it only move the equity
        base (block_entries cooldown started a new drawdown campaign there)."""
        cache_key = (bot_name, mode_group)
        with self.lock:
            if cache_key in self.cache:
                return self.cache[cache_key]
        query = db.query(Position.profit_abs, Position.closed_at).filter(
            Position.bot_name == bot_name, Position.status == "closed"
        )
        if mode_group == "backtest":
            query = query.filter(Position.mode == "backtest")
        else:
            query = query.filter(Position.mode.in_(["forward_test", "paper", "live"]))
        closed = query.order_by(Position.closed_at).all()
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
        with self.lock:
            self.cache.setdefault(cache_key, {"starting_capital": starting_capital, "peak_equity": peak_equity, "running_pnl": running, "max_dd": dd})
            return self.cache[cache_key]

    def update(self, bot_name, mode_group, profit_abs):
        """Incrementally update drawdown cache when a position closes."""
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


def unrealized_pnl(db, positions, exchange, timeframe, close_cache):
    """Open PnL of `positions` at each symbol's latest stored close (one
    Candle query per symbol per tick, memoized in `close_cache`)."""
    total = 0.0
    for p in positions:
        key = (p.exchange or exchange, p.symbol)
        if key not in close_cache:
            row = db.query(Candle.close).filter(
                Candle.exchange == key[0], Candle.symbol == p.symbol, Candle.timeframe == timeframe
            ).order_by(Candle.timestamp.desc()).first()
            close_cache[key] = float(row[0]) if row and row[0] is not None else None
        last = close_cache[key]
        if last is None:
            continue
        total += (last - (p.entry_price or 0.0)) * (p.amount or 0.0)
    return total
