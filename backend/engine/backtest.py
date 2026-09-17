"""Chronological multi-pair backtest: every whitelist symbol's candles are
merged onto one timeline and executed in order against a single cash pool
(`equity`), with fees/slippage per side, mark-to-market drawdown and the same
drawdown / capital-loss rules the live tick applies. The same loop also runs
for warm-up (run_backtest=False): it then only records indicator signals."""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
import pandas as pd
from sqlalchemy import text
from backend.models.signals import Signal
from backend.models.orders import Order
from backend.models.positions import Position
from backend.engine.sizing import _num, _int, _naive_utc, sim_frictions, calculate_trade_amount, _record_config_run
from backend.core import bot_log_buffer as blb

logger = logging.getLogger("apexalgo.bot_manager")

_BT_COMMIT_EVERY = 500  # timeline steps between backtest commits


@dataclass
class SimResult:
    starting_capital: float
    equity: float
    max_dd: float
    max_loss: float
    dd_action: str
    dd_cooldown_secs: float
    blocked_secs: float
    block_count: int
    dd_detail: dict
    timeline: list = field(default_factory=list)


def simulate(db, bot, sym_contexts, exchange_name, run_backtest, *, check_exits, position_states, states_lock, on_progress, check_abort):
    """Walk the merged timeline of `sym_contexts` (see BotManager's data prep)
    and book backtest positions/orders on `db`. `check_exits` is the engine's
    exit evaluator; `on_progress(detail, progress)` reports to the runtime
    strip; `check_abort()` raises when the user stopped the bot."""
    # Shared capital pool across ALL symbols for this bot
    bt_starting_capital = _num(bot.settings.get("backtest_capital"), 1000)
    bt_equity = bt_starting_capital  # Available cash (not locked in positions)
    bt_peak_equity = bt_starting_capital
    bt_max_dd = 0.0  # peak-to-trough on the mark-to-market equity curve
    bt_max_loss = 0.0  # worst loss of principal vs. starting capital (%)
    # Drawdown handling mirrors live: close_all (legacy) stops the bot
    # after the backtest; block_entries pauses entries until recovery
    # (dd back under half the limit) or, once flat, until the cooldown
    # has passed — the peak is then reset so the guard re-arms from the
    # new baseline instead of blocking forever on realized equity.
    bt_dd_action = bot.settings.get("drawdown_action", "close_all")
    bt_dd_limit = _num(bot.settings.get("max_drawdown"), 0)
    bt_dd_cooldown_secs = _num(bot.settings.get("drawdown_cooldown_days"), 7) * 86400
    bt_loss_limit = _num(bot.settings.get("max_capital_loss"), 0)
    bt_wind_down = False  # capital-loss breach with block_entries: no more entries, ever
    bt_entries_blocked = False
    bt_blocked_secs = 0.0
    bt_block_count = 0
    bt_flat_since = None
    bt_prev_ts = None
    # Detail for the post-backtest gate log (peak/trough of the worst dip)
    bt_peak_ts = None
    bt_dd_detail = {"peak_ts": None, "peak_eq": bt_starting_capital, "trough_ts": None, "trough_eq": bt_starting_capital, "open_at_trough": 0}

    # Fee and slippage for realistic backtest P&L
    bt_entry_fee, bt_exit_fee, bt_entry_slippage, bt_exit_slippage = sim_frictions(bot.settings)

    cooldown_trades = _int(bot.settings.get("cooldown_trades"), 0)
    cooldown_candles = _int(bot.settings.get("cooldown_candles"), 0)

    # ── Merged chronological execution across all symbols ──
    timeline = []
    for ci, ctx in enumerate(sym_contexts):
        for idx, ts_val in enumerate(list(ctx["df"]['timestamp'])):
            # Normalize sort keys — legacy rows can be tz-aware while new
            # rows are naive, and mixed values are not comparable
            timeline.append((_naive_utc(ts_val), ci, idx))
    timeline.sort(key=lambda t: (t[0], t[1]))

    max_pos = _int(bot.settings.get("max_positions"), 1)
    max_pos_scope = bot.settings.get("max_positions_scope", "per_pair")
    _tl_total = len(timeline)
    _tl_step = max(1, _tl_total // 40)
    if run_backtest:
        on_progress(f"Simulating {_tl_total} candles across {len(sym_contexts)} symbol(s)", {"done": 0, "total": _tl_total})

    for _tl_i, (_ts_key, ci, index) in enumerate(timeline):
        if run_backtest and _tl_i % _tl_step == 0:
            on_progress(f"Simulating {_tl_i}/{_tl_total} candles", {"done": _tl_i, "total": _tl_total})
            check_abort()
        if run_backtest and _tl_i and _tl_i % _BT_COMMIT_EVERY == 0:
            # Release the SQLite write lock periodically: a long
            # backtest must never make a concurrent live fill wait
            # past the busy timeout (which would book it as rejected)
            db.commit()
        ctx = sym_contexts[ci]
        symbol = ctx["symbol"]
        row = ctx["df"].iloc[index]
        ts = row['timestamp']
        if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)

        current_price = float(row['close'])
        current_open = float(row['open'])
        current_high = float(row['high'])
        current_low = float(row['low'])
        ctx["last_close"] = current_price
        just_opened_this_tick = False

        is_buy = bool(ctx["entry_arr"][index])
        is_sell = bool(ctx["exit_arr"][index])
        atr_arr = ctx["atr_arr"]
        current_atr = float(atr_arr[index]) if atr_arr is not None and not pd.isna(atr_arr[index]) else 0.0

        if run_backtest and (ctx["last_bt_ts"] is None or ts > ctx["last_bt_ts"]):
            open_bt_pos = ctx["open_pos"]

            # Cooldown check: block entry if too many trades occurred within the cooldown window
            can_buy_cooldown = True
            if cooldown_trades > 0 and cooldown_candles > 0:
                recent_trades = [idx for idx in ctx["trade_entry_indices"] if (index - idx) < cooldown_candles]
                if len(recent_trades) >= cooldown_trades:
                    can_buy_cooldown = False

            # Global scope caps the portfolio across all pairs, exactly
            # as the live gate does — a backtest of a strategy the live
            # bot is never allowed to run says nothing about it
            open_global = sum(1 for c2 in sym_contexts if c2["open_pos"] is not None)
            slot_free = max_pos_scope == "per_pair" or open_global < max_pos
            # Capital depletion halt / drawdown block: no new entries, exits keep running
            if is_buy and not open_bt_pos and slot_free and can_buy_cooldown and bt_equity > 0 and not bt_entries_blocked:
                trade_amount = calculate_trade_amount(current_price, bot.settings, current_equity=bt_equity)
                if trade_amount is not None:
                    bt_entry_price = current_price * (1 + bt_entry_slippage)
                    # Percentage sizing spends a share of equity; cap the
                    # amount so slippage + entry fee fit within the pool
                    # (100% sizing would otherwise always exceed it)
                    _entry_cfg = bot.settings.get("trade_settings", {}).get("entry", {})
                    if _entry_cfg.get("amount_type", "percentage") != "fixed":
                        max_affordable = bt_equity / (bt_entry_price * (1 + bt_entry_fee))
                        trade_amount = min(trade_amount, max_affordable)
                    investment_cost = bt_entry_price * trade_amount
                    total_cost = investment_cost * (1 + bt_entry_fee)
                    if trade_amount > 0 and total_cost <= bt_equity + 1e-9:
                        ctx["trade_entry_indices"].append(index)
                        ctx["original_amount"] = trade_amount
                        bt_equity = max(bt_equity - total_cost, 0.0)  # Lock capital + entry fee
                        open_bt_pos = Position(exchange=exchange_name, bot_name=bot.name, symbol=symbol, mode="backtest", status="open", side="long", entry_price=bt_entry_price, amount=trade_amount, created_at=_naive_utc(ts))
                        db.add(open_bt_pos)
                        db.flush()
                        db.add(Order(position_id=open_bt_pos.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=symbol, side="buy", order_type="market", price=bt_entry_price, amount=trade_amount, timestamp=_naive_utc(ts), status="filled", fee=investment_cost * bt_entry_fee))
                        ctx["open_pos"] = open_bt_pos
                        just_opened_this_tick = True

            elif open_bt_pos and not just_opened_this_tick:
                exit_events = check_exits(open_bt_pos, current_price, current_high, current_low, is_sell, bot.settings, current_atr, row_open=current_open)

                for ev in exit_events:
                    open_bt_pos = ctx["open_pos"]
                    if open_bt_pos is None: break

                    if ev.get('close_amount_type') == 'fixed':
                        close_qty = min(ev['qty_pct'], open_bt_pos.amount)
                    else:
                        # Percentage of the *original* size, so two 50% take-
                        # profits close the whole position instead of 75%
                        _base_qty = ctx["original_amount"] or open_bt_pos.amount
                        close_qty = _base_qty * (ev['qty_pct'] / 100)
                    close_qty = min(close_qty, open_bt_pos.amount)
                    if close_qty <= 0: continue

                    actual_price = ev['price'] * (1 - bt_exit_slippage)
                    db.add(Order(position_id=open_bt_pos.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=symbol, side="sell", order_type="market", price=actual_price, amount=close_qty, timestamp=_naive_utc(ts), status="filled", fee=actual_price * close_qty * bt_exit_fee))

                    entry_cost = open_bt_pos.entry_price * close_qty * (1 + bt_entry_fee)
                    exit_proceeds = actual_price * close_qty * (1 - bt_exit_fee)
                    realized_pnl = exit_proceeds - entry_cost
                    open_bt_pos.profit_abs = (open_bt_pos.profit_abs or 0.0) + realized_pnl

                    # Return sale proceeds to capital pool
                    bt_equity += exit_proceeds

                    # Weighted profit_pct: accumulate based on portion of original position closed (fee-adjusted)
                    original_amount = ctx["original_amount"]
                    if original_amount and original_amount > 0:
                        portion_pct = (realized_pnl / entry_cost) * 100 if entry_cost > 0 else 0.0
                        weight = close_qty / original_amount
                        open_bt_pos.profit_pct = (open_bt_pos.profit_pct or 0.0) + (portion_pct * weight)

                    with states_lock:
                        if open_bt_pos.id in position_states:
                            position_states[open_bt_pos.id]['triggered_exits'].add(ev['id'])
                            open_bt_pos.triggered_exits = list(position_states[open_bt_pos.id]['triggered_exits'])

                    if close_qty >= open_bt_pos.amount - 0.00001:
                        open_bt_pos.status = "closed"
                        open_bt_pos.closed_at = _naive_utc(ts)
                        with states_lock:
                            position_states.pop(open_bt_pos.id, None)
                        ctx["open_pos"] = None
                        ctx["original_amount"] = None
                    else:
                        open_bt_pos.amount -= close_qty

        if _naive_utc(ts) not in ctx["existing_timestamps"]:
            indicators = { col: float(row[col]) for col in ctx["indicator_cols"] if not pd.isna(row[col]) }
            if indicators:
                action_str = "buy" if is_buy else ("sell" if is_sell else "neutral")
                ctx["new_signals"].append(Signal(candle_id=int(row['id']), symbol=symbol, timestamp=ts, bot_name=bot.name, name="STRATEGY_TICK", action=action_str, extra_data=indicators))

        # Mark-to-market equity curve: cash + open positions at their last close
        if run_backtest:
            open_value = 0.0
            for c2 in sym_contexts:
                p2 = c2["open_pos"]
                if p2 is not None and c2["last_close"]:
                    open_value += p2.amount * c2["last_close"]
            equity_now = bt_equity + open_value
            if equity_now > bt_peak_equity:
                bt_peak_equity = equity_now
                bt_peak_ts = ts
            dd_now = ((bt_peak_equity - equity_now) / bt_peak_equity) * 100 if bt_peak_equity > 0 else 0.0
            if dd_now > bt_max_dd:
                bt_max_dd = dd_now
                bt_dd_detail = {
                    "peak_ts": bt_peak_ts, "peak_eq": bt_peak_equity,
                    "trough_ts": ts, "trough_eq": equity_now,
                    "open_at_trough": sum(1 for c2 in sym_contexts if c2["open_pos"] is not None),
                }
            if bt_starting_capital > 0:
                bt_max_loss = max(bt_max_loss, ((bt_starting_capital - equity_now) / bt_starting_capital) * 100)

            # block_entries: same rule as live — pause entries on breach;
            # resume once drawdown recovers below half the limit, or once
            # the bot has been flat for the cooldown (realized equity can't
            # recover on its own, so the peak is reset to start a new
            # campaign; max_capital_loss remains the absolute stop)
            if bt_dd_action == "block_entries" and bt_loss_limit > 0 and not bt_wind_down and bt_max_loss >= bt_loss_limit:
                # Same as live: loss of principal winds the bot down
                bt_wind_down = True
                bt_entries_blocked = True
                bt_block_count += 1
            if bt_dd_action == "block_entries" and bt_dd_limit > 0 and not bt_wind_down:
                if bt_entries_blocked and bt_prev_ts is not None:
                    bt_blocked_secs += max(0.0, (ts - bt_prev_ts).total_seconds())
                if not bt_entries_blocked and dd_now >= bt_dd_limit:
                    bt_entries_blocked = True
                    bt_block_count += 1
                    bt_flat_since = None
                elif bt_entries_blocked:
                    if open_value <= 0:
                        bt_flat_since = bt_flat_since or ts
                    else:
                        bt_flat_since = None
                    if dd_now < bt_dd_limit * 0.5:
                        bt_entries_blocked = False
                    elif bt_flat_since is not None and (ts - bt_flat_since).total_seconds() >= bt_dd_cooldown_secs:
                        bt_entries_blocked = False
                        bt_peak_equity = equity_now
                        bt_peak_ts = ts
            bt_prev_ts = ts

    # Close any trailing open backtest positions at the last available price.
    # Forward test / live must always start flat — a simulated entry must
    # never become a tracked live position.
    if run_backtest:
        for ctx in sym_contexts:
            open_bt_pos = ctx["open_pos"]
            if not open_bt_pos:
                continue
            df_s = ctx["df"]
            last_price = float(df_s.iloc[-1]['close'])
            remaining_qty = open_bt_pos.amount
            last_ts = df_s.iloc[-1]['timestamp']
            if last_ts.tzinfo is None: last_ts = last_ts.replace(tzinfo=timezone.utc)

            entry_cost = open_bt_pos.entry_price * remaining_qty * (1 + bt_entry_fee)
            exit_proceeds = last_price * remaining_qty * (1 - bt_exit_fee)
            final_pnl = exit_proceeds - entry_cost

            open_bt_pos.profit_abs = (open_bt_pos.profit_abs or 0.0) + final_pnl
            original_amount = ctx["original_amount"]
            if original_amount and original_amount > 0:
                portion_pct = (final_pnl / entry_cost) * 100 if entry_cost > 0 else 0.0
                weight = remaining_qty / original_amount
                open_bt_pos.profit_pct = (open_bt_pos.profit_pct or 0.0) + (portion_pct * weight)

            open_bt_pos.status = "closed"
            open_bt_pos.closed_at = _naive_utc(last_ts)
            bt_equity += exit_proceeds  # Return proceeds to capital pool
            db.add(Order(position_id=open_bt_pos.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=ctx["symbol"], side="sell", order_type="market", price=last_price, amount=remaining_qty, timestamp=_naive_utc(last_ts), status="filled", fee=last_price * remaining_qty * bt_exit_fee))

            with states_lock:
                position_states.pop(open_bt_pos.id, None)
            ctx["open_pos"] = None

    # Commit signals in batches — INSERT OR IGNORE respects the unique constraint
    for ctx in sym_contexts:
        new_signals = ctx["new_signals"]
        for i in range(0, len(new_signals), 500):
            batch = new_signals[i:i+500]
            for sig in batch:
                db.execute(
                    text("INSERT OR IGNORE INTO signals (candle_id, symbol, timestamp, bot_name, name, action, extra_data) VALUES (:cid, :sym, :ts, :bn, :nm, :act, :ed)"),
                    {"cid": sig.candle_id, "sym": sig.symbol, "ts": str(_naive_utc(sig.timestamp)), "bn": sig.bot_name, "nm": sig.name, "act": sig.action, "ed": json.dumps(sig.extra_data)}
                )
            db.commit()
    # Always commit — positions/orders from the backtest loop need to be persisted
    # even when there are no new signals
    db.commit()
    check_abort()
    return SimResult(
        starting_capital=bt_starting_capital, equity=bt_equity, max_dd=bt_max_dd, max_loss=bt_max_loss,
        dd_action=bt_dd_action, dd_cooldown_secs=bt_dd_cooldown_secs, blocked_secs=bt_blocked_secs,
        block_count=bt_block_count, dd_detail=bt_dd_detail, timeline=timeline,
    )


def build_summary(db, bot, res: SimResult, sym_contexts) -> dict:
    """`last_backtest_summary`: the numbers the bot card and analytics show,
    including the drawdown the gate actually enforces (the closed-trade curve
    in the UI understates intra-trade dips)."""
    closed_bt = db.query(Position.profit_abs).filter(
        Position.bot_name == bot.name, Position.mode == "backtest", Position.status == "closed"
    ).all()
    pnls = [float(p[0] or 0) for p in closed_bt]
    wins = sum(1 for p in pnls if p > 0)
    summary = {
        "trades": len(pnls),
        "wins": wins,
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(100.0 * wins / len(pnls), 1) if pnls else 0.0,
        "return_pct": round(100.0 * sum(pnls) / res.starting_capital, 2) if res.starting_capital else 0.0,
        "max_drawdown": round(res.max_dd, 2),
        "max_capital_loss": round(res.max_loss, 2),
        "entries_blocked_days": round(res.blocked_secs / 86400, 1),
        "entries_blocked_count": res.block_count,
        "candles": sum(len(c["df"]) for c in sym_contexts),
        # Data range the backtest walked — lets the analytics page
        # measure flat periods before the first / after the last trade
        "data_from": res.timeline[0][0].isoformat() if res.timeline else None,
        "data_to": res.timeline[-1][0].isoformat() if res.timeline else None,
        # Buy & hold over the same walked range, per symbol — the
        # only fair benchmark for the backtest return above
        "buy_hold": {
            c["symbol"]: {
                "first_close": round(float(c["df"]["close"].iloc[0]), 8),
                "last_close": round(float(c["df"]["close"].iloc[-1]), 8),
                "pct": round(100.0 * (float(c["df"]["close"].iloc[-1]) / float(c["df"]["close"].iloc[0]) - 1.0), 2)
                if float(c["df"]["close"].iloc[0]) > 0 else None,
            }
            for c in sym_contexts if len(c["df"]) > 0
        },
        # Distinct configurations this bot has backtested — plain
        # tweak-awareness, no judgement attached
        "variants": _record_config_run(db, bot.name, bot.settings),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    return summary


def gate_stop_reason(bot, res: SimResult):
    """Enforce max_capital_loss / max_drawdown on the simulated curve before
    the bot may go live. Returns the stop reason, or None when it may proceed
    (block_entries only warns: the same rule already paused entries inside
    the simulation)."""
    max_drawdown_pct = _num(bot.settings.get("max_drawdown"), 0)
    max_capital_loss_pct = _num(bot.settings.get("max_capital_loss"), 0)

    # Loss of principal is the hard stop regardless of drawdown_action
    if max_capital_loss_pct > 0 and res.max_loss >= max_capital_loss_pct:
        logger.warning("Bot '%s' backtest capital loss (%.2f%%) exceeds max (%.2f%%), stopping before live", bot.name, res.max_loss, max_capital_loss_pct)
        blb.push(bot.name, "WARN", f"Backtest capital loss {res.max_loss:.1f}% > {max_capital_loss_pct:.0f}% — bot stopped, not allowed to go live")
        return f"Backtest capital loss {res.max_loss:.1f}% exceeded the {max_capital_loss_pct:.0f}% limit"

    if max_drawdown_pct > 0 and res.max_dd >= max_drawdown_pct:
        _d = res.dd_detail
        _fmt = lambda t: t.strftime('%Y-%m-%d') if t is not None else '?'
        detail = (f"peak {_fmt(_d['peak_ts'])} ${_d['peak_eq']:,.0f} -> trough {_fmt(_d['trough_ts'])} "
                  f"${_d['trough_eq']:,.0f}, {_d['open_at_trough']} open position(s)")
        if res.dd_action == "block_entries":
            # Informative only: the same rule already paused entries
            # inside the simulation, so the numbers reflect it
            logger.warning("Bot '%s' backtest drawdown %.2f%% >= %.2f%% (block_entries) — going live with entries paused on breach", bot.name, res.max_dd, max_drawdown_pct)
            blb.push(bot.name, "WARN", f"Backtest max drawdown {res.max_dd:.1f}% ({detail}). Entries were blocked {res.block_count}x for {res.blocked_secs / 86400:.0f} days in total (cooldown {res.dd_cooldown_secs / 86400:.0f}d) — bot continues, new entries pause on breach.")
        else:
            logger.warning("Bot '%s' backtest drawdown (%.2f%%) exceeds max (%.2f%%), stopping before live", bot.name, res.max_dd, max_drawdown_pct)
            blb.push(bot.name, "WARN", f"Backtest max drawdown {res.max_dd:.1f}% >= {max_drawdown_pct:.0f}% ({detail}), bot stopped — not allowed to go live")
            return f"Backtest drawdown {res.max_dd:.1f}% exceeded the {max_drawdown_pct:.0f}% limit"
    return None
