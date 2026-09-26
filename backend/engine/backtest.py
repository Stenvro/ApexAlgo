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
from backend.engine.sizing import (
    _num, _int, _naive_utc, sim_frictions, calculate_trade_amount, max_affordable_amount, _record_config_run,
    backtest_pin, combined_fingerprint, slice_key, cap_by_max_order_value,
)
from backend.engine.symbols import DEFAULT_MARGIN_MODE, leverage_for, market_type_for
from backend.engine import funding, pnl, tiers
from backend.engine.capital import CapitalPools
from backend.engine.contracts import spec_for
from backend.core import bot_log_buffer as blb

logger = logging.getLogger("apexalgo.bot_manager")

_BT_COMMIT_EVERY = 500  # timeline steps between backtest commits
# Derivatives (v2.3): isolated positions are liquidated once the candle range
# reaches `spec.liquidation_price` at the maintenance-margin rate of their
# exchange tier (`tiers.mmr_for`, flat `MAINTENANCE_MARGIN` without tiers);
# cross-margin bots are liquidated as one account (`_cross_breached`);
# funding settlements stored in `funding_rates` are charged on open
# positions (`funding.payment`), marked at the candle close.


def _cap_by_max_order_value(trade_amount, price, settings, spec=None):
    """Backtest/forward mirror of the live `max_order_value` clamp: the
    quote notional of one entry never exceeds the cap (see `sizing.cap_by_max_order_value`)."""
    return cap_by_max_order_value(trade_amount, price, settings, spec)


def bot_cash_currency(sym_contexts_or_symbols, exchange_name=None) -> str | None:
    """The one cash currency a bot's whitelist is funded in (the validator
    refuses mixed lists; the first symbol decides for legacy bots)."""
    for c in sym_contexts_or_symbols:
        sym = c["symbol"] if isinstance(c, dict) else c
        if sym:
            return spec_for(exchange_name, sym).cash_currency
    return None


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
    liquidations: int = 0
    shorts: int = 0
    cash_currency: str | None = None
    console: list = field(default_factory=list)  # (level, msg) lines to push after the commit
    funding_paid: float = 0.0        # net funding booked (cash currency, negative = paid)
    funding_events: int = 0          # settlements charged on open positions
    funding_info: dict = field(default_factory=dict)  # `funding.coverage` per symbol
    mmr_source: str | None = None    # "tiers" | "flat"
    margin_mode: str | None = None


def _cross_breached(sym_contexts, cash, leverage, current_ctx, current_high, current_low):
    """Cross-margin account check on one candle: the account equity — cash
    plus every open position's margin and PnL, the current symbol marked at
    its adverse extreme (long: low, short: high), the others at their last
    close — against the sum of the maintenance margins. Returns the mark
    price per symbol when the account is liquidated, else None."""
    equity = cash
    maint = 0.0
    marks = {}
    any_open = False
    for c2 in sym_contexts:
        spec = c2["spec"]
        for p in c2["open_positions"]:
            any_open = True
            side = p.side or "long"
            if c2 is current_ctx:
                mark = current_high if side == "short" else current_low
            else:
                mark = c2["last_close"] or p.entry_price
            marks[c2["symbol"]] = mark
            equity += spec.margin(p.amount, p.entry_price, leverage) + spec.pnl_cash(side, p.amount, p.entry_price, mark)
            maint += spec.notional_cash(p.amount, mark) * tiers.mmr_for(c2.get("tiers"), tiers.tier_size(spec, p.amount, p.entry_price))
    if not any_open or equity > maint:
        return None
    return marks


def simulate(db, bot, sym_contexts, exchange_name, run_backtest, *, check_exits, position_states, states_lock, on_progress, check_abort):
    """Walk the merged timeline of `sym_contexts` (see BotManager's data prep)
    and book backtest positions/orders on `db`. `check_exits` is the engine's
    exit evaluator; `on_progress(detail, progress)` reports to the runtime
    strip; `check_abort()` raises when the user stopped the bot."""
    # Console lines are buffered and pushed by the caller after the commit:
    # `blb.push` opens a second SQLite connection, which would wait on this
    # session's uncommitted rows for the full busy timeout
    console = []
    # Every symbol is priced through its ContractSpec (spot / linear /
    # inverse); the bot's books are kept in one cash currency (validator)
    for ctx in sym_contexts:
        ctx["spec"] = spec_for(exchange_name, ctx["symbol"])
    bt_ccy = bot_cash_currency(sym_contexts, exchange_name) or "USDT"
    # Shared capital pool across ALL symbols for this bot, in `bt_ccy`
    bt_starting_capital = _num(bot.settings.get("backtest_capital"), 1000)
    pools = CapitalPools.for_bot(bt_ccy, bt_starting_capital)
    bt_equity = bt_starting_capital  # Available cash (not locked in positions) — mirrors pools.cash(bt_ccy)
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

    # Derivatives (phase 2): only the margin (notional / leverage) plus the
    # entry fee leaves the pool on open; on close the margin comes back with
    # the PnL. Every formula below keeps a literal spot branch so a spot bot
    # simulates byte-for-byte as before.
    bt_market_type = market_type_for(bot.settings)
    bt_derivative = bt_market_type != "spot"
    bt_leverage = leverage_for(bot.settings, bt_market_type) if bt_derivative else 1.0
    bt_liquidations = 0
    # Cross margin (v2.3): one account-level liquidation instead of a level
    # per position; funding and the tiered maintenance margin come from the
    # stored market data of every symbol (loaded below)
    bt_margin_mode = (bot.settings.get("margin_mode") or DEFAULT_MARGIN_MODE) if bt_derivative else None
    bt_cross = bt_margin_mode == "cross"
    bt_funding_paid = 0.0
    bt_funding_events = 0
    bt_funding_info = {}
    bt_mmr_source = None
    # Extra Order columns for derivative fills (empty on spot → same rows as before)
    _bt_open_extra = {"market_type": bt_market_type} if bt_derivative else {}
    _bt_close_extra = {"market_type": bt_market_type, "reduce_only": 1} if bt_derivative else {}
    _bt_extra_cols = {"cash_currency": bt_ccy}
    if bt_derivative:
        _tiers_syms = []
        for ctx in sym_contexts:
            df_ts = ctx["df"]["timestamp"]
            ctx["funding"] = funding.load(db, exchange_name, ctx["symbol"], df_ts.iloc[0], df_ts.iloc[-1]) if len(df_ts) else []
            ctx["tiers"] = tiers.load(db, exchange_name, ctx["symbol"])
            ctx["funding_total"] = 0.0
            bt_funding_info[ctx["symbol"]] = funding.coverage(ctx["funding"], None, None)
            if ctx["tiers"]:
                _tiers_syms.append(ctx["symbol"])
            if ctx["funding"]:
                console.append(("INFO", f"{ctx['symbol']}: {len(ctx['funding'])} funding settlements stored "
                                        f"({ctx['funding'][0][0]:%Y-%m-%d} → {ctx['funding'][-1][0]:%Y-%m-%d}) — charged on open positions"))
            else:
                console.append(("WARN", f"{ctx['symbol']}: no funding-rate data stored for this window — funding not simulated"))
        bt_mmr_source = "tiers" if _tiers_syms and len(_tiers_syms) == len(sym_contexts) else "flat"
        _has_inverse = any(c["spec"].is_inverse for c in sym_contexts)
        if bt_cross:
            _liq_note = "cross margin — liquidated as one account when the equity falls to the maintenance margin"
        elif _has_inverse:
            _liq_note = "inverse contracts liquidate on the coin-margined curve"
        else:
            _liq_note = f"isolated liquidation at about {100 * (1 - pnl.MAINTENANCE_MARGIN) / bt_leverage:.1f}% adverse move"
        _mmr_note = (f"maintenance margin from the exchange tiers of {', '.join(_tiers_syms)}" if bt_mmr_source == "tiers"
                     else f"flat {100 * pnl.MAINTENANCE_MARGIN:g}% maintenance margin (no exchange tiers stored)")
        console.append(("INFO", f"Backtest on {bt_market_type} at {bt_leverage:g}x {bt_margin_mode} in {bt_ccy}: {_liq_note}, {_mmr_note}"))
    # Shorts (phase 3): a `short` signal opens a short layer, `cover` flattens
    # the pair's shorts; both only exist when the strategy has the nodes and
    # the bot runs on a derivative market (the validator rejects them on
    # spot). Long and short never coexist on one pair — the conflicting
    # signal is ignored (INFO once per pair). Frictions come from the
    # `short`/`cover` legs, falling back to entry/exit.
    bt_shorts_enabled = bt_derivative and any(c.get("short_arr") is not None for c in sym_contexts)
    bt_s_entry_fee, bt_s_exit_fee, bt_s_entry_slippage, bt_s_exit_slippage = sim_frictions(bot.settings, "short")
    bt_short_count = 0
    _conflict_logged = set()

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

        is_buy = bool(ctx["entry_arr"][index])
        is_sell = bool(ctx["exit_arr"][index])
        is_short = bool(ctx["short_arr"][index]) if ctx.get("short_arr") is not None else False
        is_cover = bool(ctx["cover_arr"][index]) if ctx.get("cover_arr") is not None else False
        atr_arr = ctx["atr_arr"]
        current_atr = float(atr_arr[index]) if atr_arr is not None and not pd.isna(atr_arr[index]) else 0.0

        if run_backtest and (ctx["last_bt_ts"] is None or ts > ctx["last_bt_ts"]):
            open_list = ctx["open_positions"]
            spec = ctx["spec"]

            # Funding: every settlement since the position's last charge up
            # to this candle close, on its notional at the close (the
            # forward tick applies the same rule from the same table)
            if bt_derivative and ctx.get("funding") and open_list:
                for open_bt_pos in open_list:
                    _since = open_bt_pos.funding_until or open_bt_pos.created_at
                    for _f_ts, _rate in funding.settlements(ctx["funding"], _since, ts):
                        _pay = funding.payment(spec, open_bt_pos.side or "long", open_bt_pos.amount, current_price, _rate)
                        pools.charge(bt_ccy, _pay)
                        open_bt_pos.profit_abs = (open_bt_pos.profit_abs or 0.0) + _pay
                        open_bt_pos.funding_paid = (open_bt_pos.funding_paid or 0.0) + _pay
                        open_bt_pos.funding_until = _f_ts
                        _orig = ctx["original_amount"].get(open_bt_pos.id) or open_bt_pos.amount
                        _locked = spec.locked_capital(_orig, open_bt_pos.entry_price, bt_leverage,
                                                      bt_s_entry_fee if open_bt_pos.side == "short" else bt_entry_fee)
                        if _locked > 0:
                            open_bt_pos.profit_pct = (open_bt_pos.profit_pct or 0.0) + 100.0 * _pay / _locked
                        ctx["funding_total"] += _pay
                        bt_funding_paid += _pay
                        bt_funding_events += 1
                bt_equity = pools.cash(bt_ccy)

            # Cooldown check: block entry if too many trades occurred within the cooldown window
            can_buy_cooldown = True
            if cooldown_trades > 0 and cooldown_candles > 0:
                recent_trades = [idx for idx in ctx["trade_entry_indices"] if (index - idx) < cooldown_candles]
                if len(recent_trades) >= cooldown_trades:
                    can_buy_cooldown = False

            # max_positions is the pyramiding cap, exactly as the live gate
            # applies it: per_pair counts this symbol, global counts the
            # whole portfolio — a backtest of a strategy the live bot is
            # never allowed to run says nothing about it
            if max_pos_scope == "per_pair":
                slot_free = len(open_list) < max_pos
            else:
                slot_free = sum(len(c2["open_positions"]) for c2 in sym_contexts) < max_pos
            just_opened = None
            # Side already open on this pair (None when flat) — a long never
            # stacks on a short and vice versa
            pair_side = (open_list[0].side or "long") if open_list else None
            if is_buy and pair_side == "short" and symbol not in _conflict_logged:
                _conflict_logged.add(symbol)
                console.append(("INFO", f"{symbol}: BUY signal ignored while a short is open (long and short never coexist on a pair)"))
            # Capital depletion halt / drawdown block: no new entries, exits keep running
            if is_buy and pair_side != "short" and slot_free and can_buy_cooldown and bt_equity > 0 and not bt_entries_blocked:
                if bt_derivative:
                    trade_amount = calculate_trade_amount(current_price, bot.settings, current_equity=bt_equity, leverage=bt_leverage, spec=spec)
                else:
                    trade_amount = calculate_trade_amount(current_price, bot.settings, current_equity=bt_equity, spec=spec)
                trade_amount = _cap_by_max_order_value(trade_amount, current_price, bot.settings, spec)
                if trade_amount is not None:
                    bt_entry_price = current_price * (1 + bt_entry_slippage)
                    # Percentage sizing spends a share of equity; cap the
                    # amount so slippage + entry fee fit within the pool
                    # (100% sizing would otherwise always exceed it)
                    _entry_cfg = bot.settings.get("trade_settings", {}).get("entry", {})
                    if _entry_cfg.get("amount_type", "percentage") != "fixed":
                        # Margin + fee on the notional must fit the pool
                        max_affordable = max_affordable_amount(bt_equity, bt_entry_price, bt_leverage if bt_derivative else 1, bt_entry_fee, spec=spec)
                        trade_amount = min(trade_amount, max_affordable)
                    # What leaves the pool: margin + entry fee, in bt_ccy
                    total_cost = spec.locked_capital(trade_amount, bt_entry_price, bt_leverage if bt_derivative else 1, bt_entry_fee)
                    if trade_amount > 0 and total_cost <= bt_equity + 1e-9:
                        ctx["trade_entry_indices"].append(index)
                        pools.lock(bt_ccy, total_cost)  # Lock capital + entry fee
                        bt_equity = pools.cash(bt_ccy)
                        just_opened = Position(exchange=exchange_name, bot_name=bot.name, symbol=symbol, mode="backtest", status="open", side="long", entry_price=bt_entry_price, amount=trade_amount, created_at=_naive_utc(ts),
                                               contract_kind=spec.kind, contract_size=spec.contract_size, **_bt_extra_cols)
                        if bt_derivative:
                            just_opened.market_type = bt_market_type
                            just_opened.leverage = bt_leverage
                            just_opened.funding_until = _naive_utc(ts)
                        db.add(just_opened)
                        db.flush()
                        db.add(Order(position_id=just_opened.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=symbol, side="buy", order_type="market", price=bt_entry_price, amount=trade_amount, timestamp=_naive_utc(ts), status="filled", fee=spec.fee_cash(trade_amount, bt_entry_price, bt_entry_fee), fee_currency=bt_ccy, **_bt_open_extra))
                        open_list.append(just_opened)
                        ctx["original_amount"][just_opened.id] = trade_amount
            elif bt_shorts_enabled and is_short and not is_buy:
                if pair_side == "long":
                    if symbol not in _conflict_logged:
                        _conflict_logged.add(symbol)
                        console.append(("INFO", f"{symbol}: SHORT signal ignored while a long is open (long and short never coexist on a pair)"))
                elif slot_free and can_buy_cooldown and bt_equity > 0 and not bt_entries_blocked:
                    trade_amount = calculate_trade_amount(current_price, bot.settings, current_equity=bt_equity, leverage=bt_leverage, side="short", spec=spec)
                    trade_amount = _cap_by_max_order_value(trade_amount, current_price, bot.settings, spec)
                    if trade_amount is not None:
                        # Slippage works against the seller: a short fills below the close
                        bt_entry_price = current_price * (1 - bt_s_entry_slippage)
                        _short_cfg = pnl.entry_cfg(bot.settings.get("trade_settings", {}), "short")
                        if _short_cfg.get("amount_type", "percentage") != "fixed":
                            max_affordable = max_affordable_amount(bt_equity, bt_entry_price, bt_leverage, bt_s_entry_fee, spec=spec)
                            trade_amount = min(trade_amount, max_affordable)
                        total_cost = spec.locked_capital(trade_amount, bt_entry_price, bt_leverage, bt_s_entry_fee)
                        if trade_amount > 0 and total_cost <= bt_equity + 1e-9:
                            ctx["trade_entry_indices"].append(index)
                            pools.lock(bt_ccy, total_cost)  # Lock margin + entry fee
                            bt_equity = pools.cash(bt_ccy)
                            just_opened = Position(exchange=exchange_name, bot_name=bot.name, symbol=symbol, mode="backtest", status="open", side="short", entry_price=bt_entry_price, amount=trade_amount, created_at=_naive_utc(ts), market_type=bt_market_type, leverage=bt_leverage,
                                                   contract_kind=spec.kind, contract_size=spec.contract_size, funding_until=_naive_utc(ts), **_bt_extra_cols)
                            db.add(just_opened)
                            db.flush()
                            db.add(Order(position_id=just_opened.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=symbol, side="sell", order_type="market", price=bt_entry_price, amount=trade_amount, timestamp=_naive_utc(ts), status="filled", fee=spec.fee_cash(trade_amount, bt_entry_price, bt_s_entry_fee), fee_currency=bt_ccy, **_bt_open_extra))
                            open_list.append(just_opened)
                            ctx["original_amount"][just_opened.id] = trade_amount
                            bt_short_count += 1

            # Every position carries its own SL/TP/trailing state; a SELL
            # signal reaches each of them, so it flattens the whole pair.
            # The position opened on this candle is not evaluated until
            # the next one (same as live)
            for open_bt_pos in list(open_list):
                if open_bt_pos is just_opened:
                    continue
                pos_side = open_bt_pos.side or "long"
                pos_short = pos_side == "short"
                if bt_derivative and not bt_cross:
                    # Isolated: the level of this position at its tier's rate
                    _mmr = tiers.mmr_for(ctx.get("tiers"), tiers.tier_size(spec, open_bt_pos.amount, open_bt_pos.entry_price))
                    liq_price = spec.liquidation_price(pos_side, open_bt_pos.entry_price, bt_leverage, mmr=_mmr)
                    liq_hit = pnl.liquidated(pos_side, liq_price, current_high, current_low)
                else:
                    liq_price, liq_hit = None, False
                # The regular exits are evaluated first: a stop that fills
                # before the price reaches the liquidation level (e.g. an
                # SL at the open) is a normal exit. Only fills at or beyond
                # the liquidation price are impossible — those layers, and
                # whatever is still open after the exits, are liquidated.
                if pos_short:
                    exit_events = check_exits(open_bt_pos, current_price, current_high, current_low, is_cover, bot.settings, current_atr, row_open=current_open, side="short")
                else:
                    exit_events = check_exits(open_bt_pos, current_price, current_high, current_low, is_sell, bot.settings, current_atr, row_open=current_open)
                if liq_hit:
                    exit_events = [ev for ev in exit_events if not pnl.liquidated(pos_side, liq_price, ev['price'], ev['price'])]

                for ev in exit_events:
                    if open_bt_pos.status == "closed": break

                    if ev.get('close_amount_type') == 'fixed':
                        close_qty = min(ev['qty_pct'], open_bt_pos.amount)
                    else:
                        # Percentage of the *original* size, so two 50% take-
                        # profits close the whole position instead of 75%
                        _base_qty = ctx["original_amount"].get(open_bt_pos.id) or open_bt_pos.amount
                        close_qty = _base_qty * (ev['qty_pct'] / 100)
                    close_qty = min(close_qty, open_bt_pos.amount)
                    if close_qty <= 0: continue

                    _lev = bt_leverage if bt_derivative else 1
                    if pos_short:
                        # Covering buys back above the trigger; fees on both legs' notional
                        actual_price = ev['price'] * (1 + bt_s_exit_slippage)
                        _fee_in, _fee_out = bt_s_entry_fee, bt_s_exit_fee
                    else:
                        actual_price = ev['price'] * (1 - bt_exit_slippage)
                        _fee_in, _fee_out = bt_entry_fee, bt_exit_fee
                    db.add(Order(position_id=open_bt_pos.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=symbol, side=pnl.close_order_side(pos_side), order_type="market", price=actual_price, amount=close_qty, timestamp=_naive_utc(ts), status="filled", fee=spec.fee_cash(close_qty, actual_price, _fee_out), fee_currency=bt_ccy, **_bt_close_extra))
                    # What was locked for this slice (margin + entry fee), what
                    # comes back (proceeds on spot; margin + PnL − exit fee on
                    # derivatives) and the net result — all in bt_ccy
                    entry_cost = spec.locked_capital(close_qty, open_bt_pos.entry_price, _lev, _fee_in)
                    returned = spec.close_return(pos_side, close_qty, open_bt_pos.entry_price, actual_price, _lev, _fee_out)
                    realized_pnl = spec.realized_pnl(pos_side, close_qty, open_bt_pos.entry_price, actual_price, _lev, _fee_in, _fee_out)
                    open_bt_pos.profit_abs = (open_bt_pos.profit_abs or 0.0) + realized_pnl
                    pools.release(bt_ccy, returned, entry_cost)
                    bt_equity = pools.cash(bt_ccy)

                    # Weighted profit_pct: accumulate based on portion of original position closed (fee-adjusted)
                    original_amount = ctx["original_amount"].get(open_bt_pos.id)
                    if original_amount and original_amount > 0:
                        portion_pct = (realized_pnl / entry_cost) * 100 if entry_cost > 0 else 0.0
                        weight = close_qty / original_amount
                        open_bt_pos.profit_pct = (open_bt_pos.profit_pct or 0.0) + (portion_pct * weight)

                    with states_lock:
                        if open_bt_pos.id in position_states:
                            position_states[open_bt_pos.id]['triggered_exits'].add(ev['id'])
                            open_bt_pos.triggered_exits = list(position_states[open_bt_pos.id]['triggered_exits'])

                    if close_qty >= open_bt_pos.amount - pnl.close_epsilon(original_amount or open_bt_pos.amount):
                        open_bt_pos.status = "closed"
                        open_bt_pos.closed_at = _naive_utc(ts)
                        with states_lock:
                            position_states.pop(open_bt_pos.id, None)
                        open_list.remove(open_bt_pos)
                        ctx["original_amount"].pop(open_bt_pos.id, None)
                    else:
                        open_bt_pos.amount -= close_qty

                if liq_hit and open_bt_pos.status != "closed":
                    # Liquidated: the whole margin is gone, nothing returns
                    # to the pool (the entry fee was paid on open and counts
                    # against the trade; no exit fee is charged)
                    remaining_qty = open_bt_pos.amount
                    _liq_entry_fee = bt_s_entry_fee if pos_short else bt_entry_fee
                    liq_loss = spec.liquidation_loss(remaining_qty, open_bt_pos.entry_price, bt_leverage, _liq_entry_fee)
                    pools.forget(bt_ccy, liq_loss)
                    db.add(Order(position_id=open_bt_pos.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=symbol, side=pnl.close_order_side(pos_side), order_type="market", price=liq_price, amount=remaining_qty, timestamp=_naive_utc(ts), status="filled", fee=0.0, fee_currency=bt_ccy, **_bt_close_extra))
                    open_bt_pos.profit_abs = (open_bt_pos.profit_abs or 0.0) - liq_loss
                    original_amount = ctx["original_amount"].get(open_bt_pos.id)
                    if original_amount and original_amount > 0:
                        open_bt_pos.profit_pct = (open_bt_pos.profit_pct or 0.0) - 100.0 * (remaining_qty / original_amount)
                    with states_lock:
                        st = position_states.pop(open_bt_pos.id, None)
                    triggered = set((st or {}).get('triggered_exits') or ()) | set(open_bt_pos.triggered_exits or ())
                    open_bt_pos.triggered_exits = sorted(str(t) for t in triggered) + ["liquidation"]
                    open_bt_pos.status = "closed"
                    open_bt_pos.closed_at = _naive_utc(ts)
                    open_list.remove(open_bt_pos)
                    ctx["original_amount"].pop(open_bt_pos.id, None)
                    bt_liquidations += 1

            # Cross margin: after the candle's exits, the account as a whole
            # against its maintenance margin. A breach liquidates every open
            # position of the bot (all symbols) and empties the wallet; the
            # free cash lost on top of the margins is attributed to the
            # positions in proportion to their margin, so profit_pct can go
            # below −100 and the losses add up to the equity that vanished.
            if bt_cross:
                _marks = _cross_breached(sym_contexts, pools.cash(bt_ccy), bt_leverage, ctx, current_high, current_low)
                if _marks is not None:
                    _total_margin = sum(c2["spec"].margin(p.amount, p.entry_price, bt_leverage) for c2 in sym_contexts for p in c2["open_positions"])
                    _cash_lost = pools.drain(bt_ccy)
                    for c2 in sym_contexts:
                        _spec2 = c2["spec"]
                        for _p in list(c2["open_positions"]):
                            _side2 = _p.side or "long"
                            _mark = _marks.get(c2["symbol"], _p.entry_price)
                            _remaining = _p.amount
                            _margin_i = _spec2.margin(_remaining, _p.entry_price, bt_leverage)
                            _fee_i = _spec2.fee_cash(_remaining, _p.entry_price, bt_s_entry_fee if _side2 == "short" else bt_entry_fee)
                            _loss = _margin_i + _fee_i + (_cash_lost * _margin_i / _total_margin if _total_margin > 0 else 0.0)
                            db.add(Order(position_id=_p.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=c2["symbol"], side=pnl.close_order_side(_side2), order_type="market", price=_mark, amount=_remaining, timestamp=_naive_utc(ts), status="filled", fee=0.0, fee_currency=bt_ccy, **_bt_close_extra))
                            _p.profit_abs = (_p.profit_abs or 0.0) - _loss
                            _orig = c2["original_amount"].get(_p.id)
                            if _orig and _orig > 0 and (_margin_i + _fee_i) > 0:
                                _p.profit_pct = (_p.profit_pct or 0.0) - 100.0 * (_loss / (_margin_i + _fee_i)) * (_remaining / _orig)
                            with states_lock:
                                _st = position_states.pop(_p.id, None)
                            _trig = set((_st or {}).get('triggered_exits') or ()) | set(_p.triggered_exits or ())
                            _p.triggered_exits = sorted(str(t) for t in _trig) + ["liquidation"]
                            _p.status = "closed"
                            _p.closed_at = _naive_utc(ts)
                            c2["open_positions"].remove(_p)
                            c2["original_amount"].pop(_p.id, None)
                            bt_liquidations += 1
                    bt_equity = pools.cash(bt_ccy)
                    console.append(("WARN", f"{ts:%Y-%m-%d %H:%M}: cross-margin liquidation on {symbol} — account equity fell to the maintenance margin, all positions closed and the wallet ({_cash_lost:,.2f} {bt_ccy} free cash) lost"))

        if _naive_utc(ts) not in ctx["existing_timestamps"]:
            indicators = { col: float(row[col]) for col in ctx["indicator_cols"] if not pd.isna(row[col]) }
            if indicators:
                action_str = "buy" if is_buy else ("short" if is_short else ("sell" if is_sell else ("cover" if is_cover else "neutral")))
                ctx["new_signals"].append(Signal(candle_id=int(row['id']), symbol=symbol, timestamp=ts, bot_name=bot.name, name="STRATEGY_TICK", action=action_str, extra_data=indicators))

        # Mark-to-market equity curve: cash + open positions at their last close
        if run_backtest:
            open_value = 0.0
            for c2 in sym_contexts:
                if c2["last_close"]:
                    open_value += c2["spec"].mark_value(c2["open_positions"], c2["last_close"], bt_leverage if bt_derivative else 1)
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
                    "open_at_trough": sum(len(c2["open_positions"]) for c2 in sym_contexts),
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
            df_s = ctx["df"]
            last_price = float(df_s.iloc[-1]['close'])
            last_ts = df_s.iloc[-1]['timestamp']
            if last_ts.tzinfo is None: last_ts = last_ts.replace(tzinfo=timezone.utc)
            spec = ctx["spec"]
            _lev = bt_leverage if bt_derivative else 1
            for open_bt_pos in list(ctx["open_positions"]):
                remaining_qty = open_bt_pos.amount
                pos_side = open_bt_pos.side or "long"
                pos_short = pos_side == "short"
                _fee_in, _fee_out = (bt_s_entry_fee, bt_s_exit_fee) if pos_short else (bt_entry_fee, bt_exit_fee)

                entry_cost = spec.locked_capital(remaining_qty, open_bt_pos.entry_price, _lev, _fee_in)
                final_pnl = spec.realized_pnl(pos_side, remaining_qty, open_bt_pos.entry_price, last_price, _lev, _fee_in, _fee_out)
                open_bt_pos.profit_abs = (open_bt_pos.profit_abs or 0.0) + final_pnl
                original_amount = ctx["original_amount"].get(open_bt_pos.id)
                if original_amount and original_amount > 0:
                    portion_pct = (final_pnl / entry_cost) * 100 if entry_cost > 0 else 0.0
                    weight = remaining_qty / original_amount
                    open_bt_pos.profit_pct = (open_bt_pos.profit_pct or 0.0) + (portion_pct * weight)

                open_bt_pos.status = "closed"
                open_bt_pos.closed_at = _naive_utc(last_ts)
                pools.release(bt_ccy, spec.close_return(pos_side, remaining_qty, open_bt_pos.entry_price, last_price, _lev, _fee_out), entry_cost)
                bt_equity = pools.cash(bt_ccy)
                db.add(Order(position_id=open_bt_pos.id, exchange=exchange_name, bot_name=bot.name, mode="backtest", symbol=ctx["symbol"], side=pnl.close_order_side(pos_side), order_type="market", price=last_price, amount=remaining_qty, timestamp=_naive_utc(last_ts), status="filled", fee=spec.fee_cash(remaining_qty, last_price, _fee_out), fee_currency=bt_ccy, **_bt_close_extra))

                with states_lock:
                    position_states.pop(open_bt_pos.id, None)
            ctx["open_positions"] = []
            ctx["original_amount"] = {}

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
        block_count=bt_block_count, dd_detail=bt_dd_detail, timeline=timeline, liquidations=bt_liquidations,
        shorts=bt_short_count, cash_currency=bt_ccy, console=console,
        funding_paid=bt_funding_paid, funding_events=bt_funding_events, funding_info=bt_funding_info,
        mmr_source=bt_mmr_source, margin_mode=bt_margin_mode,
    )


def build_summary(db, bot, res: SimResult, sym_contexts, exchange_name=None) -> dict:
    """`last_backtest_summary`: the numbers the bot card and analytics show,
    including the drawdown the gate actually enforces (the closed-trade curve
    in the UI understates intra-trade dips), plus the run's identity — slice,
    data hash and both variant counters — for reproducibility."""
    closed_bt = db.query(Position.profit_abs).filter(
        Position.bot_name == bot.name, Position.mode == "backtest", Position.status == "closed"
    ).all()
    pnls = [float(p[0] or 0) for p in closed_bt]
    wins = sum(1 for p in pnls if p > 0)

    data_from = _naive_utc(res.timeline[0][0]) if res.timeline else None
    data_to = _naive_utc(res.timeline[-1][0]) if res.timeline else None
    # Slice = the candles walked: the pinned window when there is one, else
    # the realized range (which moves with every unpinned run)
    pin_from, pin_to = backtest_pin(bot.settings)
    pinned = pin_from is not None
    window_from, window_to = (pin_from, pin_to) if pinned else (data_from, data_to)
    hash_by_symbol = {c["symbol"]: c["data_hash"] for c in sym_contexts if c.get("data_hash")}
    data_hash = combined_fingerprint(hash_by_symbol)
    slice_hash = slice_key(exchange_name or bot.settings.get("data_exchange", "okx"),
                           [c["symbol"] for c in sym_contexts], bot.settings.get("timeframe"), window_from, window_to)
    # Same slice as the previous saved run but different candles underneath
    # (re-download, gap repair, exchange restatement) — None when the slice
    # moved, because then a differing hash says nothing
    prev = (bot.settings or {}).get("last_backtest_summary") or {}
    data_changed = (prev["data_hash"] != data_hash) if prev.get("data_hash") and prev.get("slice_key") == slice_hash else None
    variants, variants_on_slice = _record_config_run(db, bot.name, bot.settings, slice_hash, data_hash, window_from, window_to)
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
        "data_from": data_from.isoformat() if data_from else None,
        "data_to": data_to.isoformat() if data_to else None,
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
        # Distinct configurations this bot has backtested — ever, and on
        # exactly this slice of data — plain tweak-awareness, no judgement
        "variants": variants,
        "variants_on_slice": variants_on_slice,
        # Run identity: pin + slice + raw-candle hash make the run reproducible
        # and let the next run on the same slice detect altered data
        "pinned": pinned,
        "window_from": window_from.isoformat() if window_from else None,
        "window_to": window_to.isoformat() if window_to else None,
        "slice_key": slice_hash,
        "data_hash": data_hash,
        "data_hash_by_symbol": hash_by_symbol,
        "data_changed": data_changed,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    # The currency every money figure above is in (backtest_capital, net_pnl,
    # the drawdown peak/trough): the whitelist's cash currency
    ccy = res.cash_currency or bot_cash_currency(sym_contexts, exchange_name)
    if ccy:
        summary["cash_currency"] = ccy
    market_type = market_type_for(bot.settings)
    if market_type != "spot":
        summary.update({
            "market_type": market_type,
            "leverage": leverage_for(bot.settings, market_type),
            "margin_mode": res.margin_mode or (bot.settings.get("margin_mode") or DEFAULT_MARGIN_MODE),
            "liquidations": res.liquidations,
            # Funding: "simulated" when settlements were stored for every
            # symbol of the window, "partial"/"no data" otherwise; the net
            # amount booked is inside net_pnl already
            "funding": _funding_status(res.funding_info),
            "funding_paid": round(res.funding_paid, 8),
            "funding_events": res.funding_events,
            "funding_by_symbol": res.funding_info,
            "mmr_source": res.mmr_source or "flat",
        })
        if res.shorts:
            # Closed short trades (buy & hold above stays the long reference on purpose)
            short_closed = db.query(Position.id).filter(
                Position.bot_name == bot.name, Position.mode == "backtest", Position.status == "closed", Position.side == "short"
            ).count()
            summary["short_trades"] = short_closed
            summary["long_trades"] = len(pnls) - short_closed
    return summary


def _funding_status(info: dict) -> str:
    states = {v.get("funding") for v in (info or {}).values()}
    if not states or states == {"no data"}:
        return "no data"
    return "simulated" if states == {"simulated"} else "partial"


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
        _ccy = res.cash_currency or ""
        detail = (f"peak {_fmt(_d['peak_ts'])} {_d['peak_eq']:,.2f} {_ccy} -> trough {_fmt(_d['trough_ts'])} "
                  f"{_d['trough_eq']:,.2f} {_ccy}, {_d['open_at_trough']} open position(s)")
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
