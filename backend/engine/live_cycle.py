"""One live candle for every bot on a subscription: entry (maybe_open_position),
exits, risk gates and the signal record. Each function takes the BotManager as
`engine` for its locks, caches and the exchange seam; nothing here is async."""
import json
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import pandas as pd
import ccxt
from sqlalchemy import text, func, or_, and_
from sqlalchemy.orm import selectinload
from backend.core.database import SessionLocal
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.orders import Order
from backend.models.positions import Position
from backend.models.exchange_keys import ExchangeKey
from backend.engine.evaluator import NodeEvaluator
from backend.engine.sizing import _indicator_fingerprint, _num, _int, _tf_seconds, _naive_utc, cap_by_max_order_value, position_spec
from backend.engine.symbols import DEFAULT_MARGIN_MODE, base_of, is_derivative, leverage_for, market_type_for, normalize
from backend.engine import funding, tiers
from backend.engine import broker, pnl, risk
from backend.engine.contracts import spec_for, spec_for_instance
from backend.core.exchange_registry import market_caps
from backend.core import bot_log_buffer as blb

logger = logging.getLogger("apexalgo.bot_manager")


# ── Derivative order plumbing ─────────────────────────────────────────────
# Spot orders keep their literal create_market_buy/sell_order calls; the
# helpers below only run for `BASE/QUOTE:SETTLE` symbols, where ccxt wants
# the amount in contracts, closes must be reduce-only, and a few exchanges
# take the leverage per order.

def _spec(ccxt_inst, exchange, ccxt_symbol):
    """ContractSpec for a symbol: from the exchange instance's market when
    there is one, else the registry cache / the symbol's own shape."""
    if ccxt_inst is not None:
        return spec_for_instance(ccxt_inst, ccxt_symbol)
    return spec_for(exchange, ccxt_symbol)


def _pos_spec(pos):
    """ContractSpec a stored position was booked with."""
    return position_spec(pos.symbol, pos.contract_kind, pos.contract_size, pos.exchange)


def _precise_amount(ccxt_inst, ccxt_symbol, qty):
    """Round a size to the market's precision. On derivatives the precision
    is defined in contracts, so convert, round, convert back."""
    if not is_derivative(ccxt_symbol):
        return float(ccxt_inst.amount_to_precision(ccxt_symbol, qty))
    spec = spec_for_instance(ccxt_inst, ccxt_symbol)
    contracts = float(ccxt_inst.amount_to_precision(ccxt_symbol, spec.to_contracts(qty)))
    return spec.from_contracts(contracts)


def _place_market_order(ccxt_inst, api_key_record, ccxt_symbol, side, qty, *, reduce_only, leverage=1):
    """Market order of `qty` (base units on spot/linear, contracts on
    inverse). Spot: the pre-existing ccxt shortcuts. Derivatives:
    `create_order` in contracts, reduce-only on closes and, where the
    exchange wants it, the leverage in the params."""
    if not is_derivative(ccxt_symbol):
        if side == "buy":
            return ccxt_inst.create_market_buy_order(ccxt_symbol, qty)
        return ccxt_inst.create_market_sell_order(ccxt_symbol, qty)
    contracts = spec_for_instance(ccxt_inst, ccxt_symbol).to_contracts(qty)
    params = {}
    if reduce_only:
        params["reduceOnly"] = True
    caps = market_caps(getattr(api_key_record, "exchange", ""), "swap")
    if caps is not None and caps.leverage_in_order:
        params["leverage"] = int(float(leverage or 1))
    return ccxt_inst.create_order(ccxt_symbol, "market", side, contracts, None, params)


def _filled_base(ccxt_inst, ccxt_symbol, ex_order) -> float:
    """`filled` of a ccxt order in the position's units (contracts x
    contract size on linear perpetuals, contracts as-is on inverse)."""
    filled = float(ex_order.get("filled") or 0)
    if filled > 0 and is_derivative(ccxt_symbol):
        return spec_for_instance(ccxt_inst, ccxt_symbol).from_contracts(filled)
    return filled


def _fee_cols(fee_info, cash_ccy):
    """`fee_currency`/`fee_cash` Order columns from a ccxt fee dict (what
    the exchange charged, before conversion into the cash currency)."""
    if isinstance(fee_info, dict) and fee_info.get("cost") is not None:
        try:
            return {"fee_currency": str(fee_info.get("currency") or cash_ccy).upper(), "fee_cash": float(fee_info.get("cost"))}
        except (TypeError, ValueError):
            pass
    return {"fee_currency": cash_ccy}


def _entry_fee_total(pos, open_side):
    """Entry fees paid on the position's opening fills (cash currency)."""
    return sum((o.fee or 0.0) for o in (pos.orders or []) if o.side == open_side and o.status == "filled")


def _original_amount(pos, open_side):
    """Size the position was opened with (sum of its opening fills)."""
    return sum(o.amount for o in (pos.orders or []) if o.side == open_side and o.status == "filled") or pos.amount


def _book_liquidation(engine, db, bot, pos, price, ts, dd_group, note, extra_loss=0.0):
    """Close `pos` as liquidated: the remaining margin and its share of the
    entry fee are lost, nothing comes back to the pool, no exit fee (same
    booking as the backtest). Adds the reduce-only close order at `price`
    and updates the drawdown state. `extra_loss` is the share of the free
    cash a cross-margin liquidation takes on top of the margin, so the
    booked losses add up to the equity that vanished."""
    pos_side = pos.side or "long"
    open_side = pnl.open_order_side(pos_side)
    remaining = float(pos.amount or 0.0)
    original = _original_amount(pos, open_side)
    lev = max(float(pos.leverage or 1), 1.0)
    spec = _pos_spec(pos)
    fee_portion = _entry_fee_total(pos, open_side) * (remaining / original) if original > 0 else 0.0
    own_loss = spec.margin(remaining, pos.entry_price or 0.0, lev) + fee_portion
    loss = own_loss + float(extra_loss or 0.0)
    close_cols = {"market_type": pos.market_type or "swap", "reduce_only": 1, "fee_currency": spec.cash_currency}
    db.add(Order(position_id=pos.id, exchange=pos.exchange, bot_name=bot.name, mode=pos.mode, symbol=pos.symbol,
                 side=pnl.close_order_side(pos_side), order_type="market", price=price, amount=remaining,
                 timestamp=ts, exchange_order_id=f"liq_{uuid.uuid4().hex[:8]}", status="filled", fee=0.0, **close_cols))
    pos.profit_abs = (pos.profit_abs or 0.0) - loss
    if original > 0 and own_loss > 0:
        pos.profit_pct = (pos.profit_pct or 0.0) - 100.0 * (loss / own_loss) * (remaining / original)
    with engine._position_states_lock:
        st = engine.position_states.pop(pos.id, None)
    triggered = set((st or {}).get("triggered_exits") or ()) | set(pos.triggered_exits or ())
    pos.triggered_exits = sorted(str(t) for t in triggered) + ["liquidation"]
    pos.status = "closed"
    pos.closed_at = ts
    engine._update_drawdown(bot.name, dd_group, -loss)
    # Commit before the console line: the log buffer writes on its own
    # connection and would otherwise wait on this session's rows
    db.commit()
    logger.warning("%s LIQUIDATION %s #%d: %s @ %s (loss %.2f %s) — %s", pos.mode.upper(), pos.symbol, pos.id, remaining, price, loss, spec.cash_currency, note)
    blb.push(bot.name, "WARN", f"{pos.mode.upper()} LIQUIDATION {pos.symbol}: {remaining:g} @ {price:g} (−{loss:.2f} {spec.cash_currency}) — {note}")
    return loss


def _apply_forward_funding(engine, db, bot, positions, exchange, ccxt_symbol, price, candle_ts, dd_group, events_cache):
    """Forward test: charge every stored funding settlement a position has
    not been charged for yet (`funding_until < ts <= candle_ts`) on its
    notional at `price` — the backtest rule, from the same table. Returns
    the net amount booked."""
    key = (exchange, ccxt_symbol)
    if key not in events_cache:
        events_cache[key] = funding.load(db, exchange, ccxt_symbol)
    events = events_cache[key]
    if not events:
        return 0.0
    total = 0.0
    for pos in positions:
        if pos.status != "open":
            continue
        pos_side = pos.side or "long"
        spec = _pos_spec(pos)
        open_side = pnl.open_order_side(pos_side)
        original = _original_amount(pos, open_side)
        lev = max(float(pos.leverage or 1), 1.0)
        locked = spec.margin(original, pos.entry_price or 0.0, lev) + _entry_fee_total(pos, open_side) if original > 0 else 0.0
        booked = 0.0
        for f_ts, rate in funding.settlements(events, pos.funding_until or pos.created_at, candle_ts):
            pay = funding.payment(spec, pos_side, pos.amount or 0.0, price, rate)
            pos.profit_abs = (pos.profit_abs or 0.0) + pay
            pos.funding_paid = (pos.funding_paid or 0.0) + pay
            if locked > 0:
                pos.profit_pct = (pos.profit_pct or 0.0) + 100.0 * pay / locked
            pos.funding_until = f_ts
            booked += pay
        if booked:
            engine._update_drawdown(bot.name, dd_group, booked)
            total += booked
            blb.push(bot.name, "INFO", f"FORWARD_TEST funding {pos.symbol} #{pos.id}: {booked:+.4f} {spec.cash_currency}")
    return total


def _forward_cross_breached(db, engine, bot, positions, exchange, timeframe, ccxt_symbol, current_high, current_low, candle_ts, close_cache):
    """Cross-margin account check of a forward-test bot on this candle:
    `forward_pool` free cash plus every open position's margin and PnL —
    the current symbol at its adverse extreme, the others at their stored
    close at or before the candle — against the sum of the maintenance
    margins. Returns `(marks, cash)` when liquidated, else None."""
    if not positions:
        return None
    ccy = _pos_spec(positions[0]).cash_currency
    cash = engine._forward_pool(db, bot, ccy)
    equity = cash
    maint = 0.0
    marks = {}
    for p in positions:
        side = p.side or "long"
        spec = _pos_spec(p)
        lev = max(float(p.leverage or 1), 1.0)
        if normalize(p.symbol) == ccxt_symbol:
            mark = current_high if side == "short" else current_low
        else:
            key = (p.exchange or exchange, p.symbol)
            if key not in close_cache:
                row = db.query(Candle.close).filter(
                    Candle.exchange == key[0], Candle.symbol == p.symbol, Candle.timeframe == timeframe,
                    Candle.timestamp <= candle_ts).order_by(Candle.timestamp.desc()).first()
                close_cache[key] = float(row[0]) if row and row[0] is not None else None
            mark = close_cache[key] if close_cache[key] is not None else (p.entry_price or 0.0)
        marks[p.id] = mark
        equity += spec.margin(p.amount or 0.0, p.entry_price or 0.0, lev) + spec.pnl_cash(side, p.amount or 0.0, p.entry_price or 0.0, mark)
        maint += spec.notional_cash(p.amount or 0.0, mark) * tiers.mmr_for(
            tiers.load(db, p.exchange or exchange, normalize(p.symbol)), tiers.tier_size(spec, p.amount or 0.0, p.entry_price or 0.0))
    if equity > maint:
        return None
    return marks, max(cash, 0.0)


def close_all_open_positions(engine, bot, db, key_records):
    """Market-close every open non-backtest position for a bot before it is
    force-stopped. A stopped bot no longer evaluates SL/TP, so leaving live
    positions open would mean unmanaged, unbounded exposure."""
    open_positions = db.query(Position).options(selectinload(Position.orders)).filter(
        Position.bot_name == bot.name,
        Position.status == "open",
        Position.mode.in_(["forward_test", "paper", "live"]),
    ).all()
    if not open_positions:
        return

    api_key_record = None
    if bot.settings.get("api_execution") and bot.settings.get("api_key_name"):
        api_key_record = key_records.get(bot.settings.get("api_key_name"))

    ccxt_inst = None
    if api_key_record and any(p.mode in ("paper", "live") for p in open_positions):
        try:
            ccxt_inst = engine._get_ccxt_instance(api_key_record)
            ccxt_inst.load_markets()
        except Exception as exc:
            logger.error("Could not build exchange client to close positions for '%s': %s", bot.name, exc, exc_info=True)
    _ccxt_by_symbol = {}

    def _client_for(sym):
        # Inverse contracts on a separate ccxt class (binancecoinm) need their own instance
        if sym not in _ccxt_by_symbol:
            inst = ccxt_inst
            if api_key_record and engine._needs_own_instance(api_key_record, sym):
                try:
                    inst = engine._get_ccxt_instance(api_key_record, sym)
                    inst.load_markets()
                except Exception as exc:
                    logger.error("Could not build exchange client for %s: %s", sym, exc, exc_info=True)
                    inst = None
            _ccxt_by_symbol[sym] = inst
        return _ccxt_by_symbol[sym]

    now_ts = _naive_utc(datetime.now(timezone.utc))
    timeframe = bot.settings.get("timeframe")

    for pos in open_positions:
        last_candle = db.query(Candle.close).filter(
            Candle.exchange == pos.exchange, Candle.symbol == pos.symbol, Candle.timeframe == timeframe
        ).order_by(Candle.timestamp.desc()).first()
        close_price = float(last_candle[0]) if last_candle else pos.entry_price

        close_qty = pos.amount
        actual_price = close_price
        actual_fee = 0.0
        order_id = f"local_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        ccxt_symbol = normalize(pos.symbol)
        spec = _pos_spec(pos)
        _close_cols = {"market_type": "swap", "reduce_only": 1} if is_derivative(ccxt_symbol) else {}
        _close_cols["fee_currency"] = spec.cash_currency
        # A short is flattened with a reduce-only buy; PnL flips sign
        pos_side = pos.side or "long"
        _pos_open_side, _pos_close_side = pnl.open_order_side(pos_side), pnl.close_order_side(pos_side)

        if pos.mode == "forward_test":
            # Backtest exit economics: slippage against the fill, fee on the proceeds
            _, exit_fee_pct, _, exit_slip = engine._sim_frictions(bot.settings, pos_side)
            actual_price = close_price * (1 + exit_slip) if pos_side == "short" else close_price * (1 - exit_slip)
            actual_fee = spec.fee_cash(close_qty, actual_price, exit_fee_pct)
        if pos.mode in ("paper", "live"):
            ccxt_inst = _client_for(ccxt_symbol)
            if ccxt_inst is None:
                logger.error("Cannot close %s position on %s for '%s': no exchange client. Position left open.", pos.mode, pos.symbol, bot.name)
                blb.push(bot.name, "ERROR", f"Could not close {pos.mode} position on {pos.symbol}: exchange unavailable — close it manually!")
                continue
            try:
                sell_qty = _precise_amount(ccxt_inst, ccxt_symbol, close_qty)
                if sell_qty <= 0:
                    continue
                ex_order = _place_market_order(ccxt_inst, api_key_record, ccxt_symbol, _pos_close_side, sell_qty, reduce_only=True, leverage=pos.leverage)
                ex_order = engine._reconcile_order(ccxt_inst, ex_order, ccxt_symbol)
                filled_qty = _filled_base(ccxt_inst, ccxt_symbol, ex_order)
                if filled_qty <= 0 and ex_order.get("status") != "closed":
                    db.add(Order(position_id=pos.id, exchange=pos.exchange, bot_name=bot.name, mode=pos.mode, symbol=pos.symbol, side=_pos_close_side, order_type="market", price=close_price, amount=sell_qty, timestamp=now_ts, exchange_order_id=ex_order.get("id"), status="canceled", **_close_cols))
                    db.commit()
                    blb.push(bot.name, "ERROR", f"Forced close on {pos.symbol} did not fill; position left open — close it manually!")
                    continue
                close_qty = filled_qty if filled_qty > 0 else sell_qty
                actual_price = ex_order.get("average") or ex_order.get("price") or close_price
                order_id = ex_order.get("id") or order_id
                actual_fee = engine._fee_in_quote(ex_order.get("fee"), ccxt_symbol, actual_price, ccxt_inst)
                _close_cols.update(_fee_cols(ex_order.get("fee"), spec.cash_currency))
            except Exception as exc:
                logger.error("Forced close failed for '%s' on %s: %s", bot.name, pos.symbol, exc, exc_info=True)
                blb.push(bot.name, "ERROR", f"Forced close failed on {pos.symbol}: {exc} — close it manually!")
                continue

        # Same booking as the tick's exit: proportional entry fee + exit fee,
        # profit_pct on the capital the leg had locked (margin + entry fee)
        original_amount = _original_amount(pos, _pos_open_side)
        entry_fee_portion = _entry_fee_total(pos, _pos_open_side) * (close_qty / original_amount) if original_amount > 0 else 0.0
        realized_pnl = pnl.price_pnl(pos_side, pos.entry_price, actual_price, close_qty, spec=spec) - entry_fee_portion - actual_fee
        pos.profit_abs = (pos.profit_abs or 0.0) + realized_pnl
        if pos.entry_price and original_amount > 0:
            locked = pnl.locked_capital(pos.entry_price, close_qty, pos.leverage or 1, spec=spec) + entry_fee_portion
            pos.profit_pct = (pos.profit_pct or 0.0) + ((realized_pnl / locked) * 100 if locked > 0 else 0.0) * (close_qty / original_amount)
        engine._update_drawdown(bot.name, risk.mode_group_for(pos.mode), realized_pnl)

        db.add(Order(position_id=pos.id, exchange=pos.exchange, bot_name=bot.name, mode=pos.mode, symbol=pos.symbol, side=_pos_close_side, order_type="market", price=actual_price, amount=close_qty, timestamp=now_ts, exchange_order_id=order_id, status="filled", fee=actual_fee, **_close_cols))

        if close_qty >= pos.amount - pnl.close_epsilon(original_amount):
            pos.status = "closed"
            pos.closed_at = now_ts
            with engine._position_states_lock:
                engine.position_states.pop(pos.id, None)
        else:
            pos.amount -= close_qty
            blb.push(bot.name, "WARN", f"Partial forced close on {pos.symbol}: {close_qty} {'covered' if pos_side == 'short' else 'sold'}, {pos.amount} still open — close it manually!")

        # Committed before the console line (the log buffer writes on its own connection)
        db.commit()

        logger.info("Forced close [%s] %s: %s @ %s (PnL %+.2f %s)", pos.mode, pos.symbol, close_qty, actual_price, realized_pnl, spec.cash_currency)
        blb.push(bot.name, "INFO", f"Forced close [{pos.mode}] {pos.symbol}: {close_qty} @ {actual_price} (PnL {realized_pnl:+.2f} {spec.cash_currency})")


def maybe_open_position(engine, db, bot, exchange, symbol, mode, api_key_record, get_ccxt,
                         is_buy, entries_blocked, bot_positions, open_count, max_pos, can_buy_cooldown,
                         current_price, latest_time, side="long"):
    """Entry leg of one live tick. Returns the freshly opened Position, or
    None when no entry was made (no signal, blocked, skipped, or failed).
    Kept separate from the exit loop on purpose: bailing out of the entry
    must never skip the SL/TP evaluation of the positions already open.
    `side="short"` opens a short (derivatives only): the same sizing and
    safety path with a non-reduce-only SELL, slippage against the seller and
    the `short` leg of trade_settings; `is_buy` is then the short signal."""
    ccxt_symbol = normalize(symbol)
    # Derivatives: leverage from the bot (the linked key fixes the market
    # type); every spot bot goes through the unchanged branches below
    _market_type = market_type_for(bot.settings, api_key_record)
    _derivative = is_derivative(ccxt_symbol)
    _leverage = leverage_for(bot.settings, _market_type) if _derivative else 1.0
    # Contract economics (spot / linear / inverse) and the cash currency
    # every money figure below is in
    spec = _spec(get_ccxt() if (api_key_record and mode in ("paper", "live")) else None, exchange, ccxt_symbol)
    _quote = spec.cash_currency
    _open_cols = {"market_type": _market_type} if _derivative else {}
    _open_cols["fee_currency"] = _quote
    _short = side == "short"
    _label = "SHORT" if _short else "BUY"
    _open_side = pnl.open_order_side(side)
    _entry_cfg = pnl.entry_cfg(bot.settings.get("trade_settings", {}), side)
    if _short and not _derivative:
        # The validator rejects short nodes on spot; never sell what isn't held
        if is_buy:
            blb.push(bot.name, "ERROR", f"SHORT signal on {symbol} ignored — shorts need a perpetual (swap) market")
        return None
    if is_buy and entries_blocked:
        blb.push(bot.name, "INFO", f"{_label} signal on {symbol} skipped — entries blocked by max drawdown")
    elif is_buy and open_count >= max_pos:
        # Pyramiding cap, same rule as the backtest (per_pair: this symbol,
        # global: the whole portfolio)
        blb.push(bot.name, "INFO", f"{_label} signal on {symbol} skipped — max_positions ({max_pos}) reached")
    elif is_buy and can_buy_cooldown:
        if _derivative:
            trade_amount = engine._calculate_trade_amount(current_price, bot.settings, leverage=_leverage, side=side, spec=spec)
        else:
            trade_amount = engine._calculate_trade_amount(current_price, bot.settings, spec=spec)
        if trade_amount is None:
            logger.warning("Skipping %s for %s: invalid trade amount", _label.lower(), symbol)
        else:
            try:
                actual_price = current_price
                order_id = f"local_{int(latest_time.timestamp())}_{uuid.uuid4().hex[:8]}"

                buy_fee = 0.0
                if mode == "forward_test":
                    # Same economics as the backtest: size from the carried
                    # pool, pay entry slippage and fee. A forward test that
                    # trades frictionless on the original capital would
                    # confirm a strategy the backtest never ran.
                    pool = engine._forward_pool(db, bot, _quote)
                    if pool <= 0:
                        blb.push(bot.name, "WARN", f"{_label} signal on {symbol} skipped — forward-test pool depleted ({pool:,.2f} {_quote})")
                        return None
                    entry_fee_pct, _, entry_slip, _ = engine._sim_frictions(bot.settings, side)
                    _lev = _leverage if _derivative else 1
                    # Margin (notional / leverage) + fee on the notional must fit the pool
                    trade_amount = engine._calculate_trade_amount(current_price, bot.settings, current_equity=pool, leverage=_lev, side=side, spec=spec)
                    if trade_amount is None:
                        return None
                    # Slippage works against the taker: a short fills below the close
                    actual_price = current_price * (1 - entry_slip) if _short else current_price * (1 + entry_slip)
                    if _entry_cfg.get("amount_type", "percentage") != "fixed":
                        trade_amount = min(trade_amount, spec.max_affordable_qty(pool, actual_price, _lev, entry_fee_pct))
                    if trade_amount <= 0 or spec.locked_capital(trade_amount, actual_price, _lev, entry_fee_pct) > pool + 1e-9:
                        blb.push(bot.name, "WARN", f"{_label} signal on {symbol} skipped — entry does not fit the forward-test pool ({pool:,.2f} {_quote})")
                        return None
                    # Same safety cap as live and the backtest (quote notional)
                    trade_amount = cap_by_max_order_value(trade_amount, current_price, bot.settings, spec)
                    buy_fee = spec.fee_cash(trade_amount, actual_price, entry_fee_pct)
                elif mode in ["paper", "live"] and api_key_record:
                    ccxt_inst = get_ccxt()

                    # A restart replays the last candle: never place a second
                    # BUY for a candle that already produced one
                    existing_buy = db.query(Order.id).filter(
                        Order.bot_name == bot.name, Order.symbol == symbol,
                        Order.mode == mode, Order.side == _open_side,
                        Order.timestamp == latest_time,
                        # Closes are reduce-only: a cover on the same candle is a
                        # buy, a long's exit a sell — neither is an entry
                        func.coalesce(Order.reduce_only, 0) == 0,
                    ).first()
                    if existing_buy:
                        logger.info("%s %s for %s @ %s already recorded, skipping duplicate entry", mode.upper(), _label, symbol, latest_time)
                        return None
                    # Demo accounts list fewer pairs than the public market
                    # the backtest ran on — an order there can only fail
                    if ccxt_inst.markets and ccxt_symbol not in ccxt_inst.markets:
                        logger.warning("%s %s skipped: %s is not listed on %s for key '%s'", mode.upper(), _label, ccxt_symbol, api_key_record.exchange, api_key_record.name)
                        blb.push(bot.name, "WARN", f"{_label} signal on {symbol} skipped — not listed on {api_key_record.exchange.upper()}{' demo' if api_key_record.is_sandbox else ''} for key '{api_key_record.name}'")
                        return None

                    # Size trades from the wallet: this bot's share
                    # (live_allocation_pct) of the quote equity, minus
                    # what it already has deployed — same pool logic
                    # as the backtest's bt_equity
                    free_balance = engine._get_live_capital(ccxt_inst, api_key_record, ccxt_symbol, bot.name)
                    if free_balance is None:
                        if mode == "live":
                            logger.warning("Skipping entry for %s: could not verify exchange balance", symbol)
                            blb.push(bot.name, "WARN", "Skipping entry: could not verify exchange balance")
                            return None
                        sizing_capital = _num(bot.settings.get("backtest_capital"), 1000)
                        logger.info("Bot '%s': sandbox balance unavailable, sizing paper entry from backtest capital %.2f %s", bot.name, sizing_capital, _quote)
                    else:
                        pool, wallet_total, bot_total = engine._live_allocation(db, bot, _quote, free_balance)
                        if pool <= 0:
                            blb.push(bot.name, "WARN", f"{_label} signal on {symbol} skipped — allocation fully deployed ({bot_total:,.0f} {_quote} = {_num(bot.settings.get('live_allocation_pct'), 100):.0f}% of wallet {wallet_total:,.0f})")
                            return None
                        sizing_capital = min(free_balance, pool)
                        logger.info("Bot '%s': sizing %s entry from %.2f %s (free=%.2f, pool remaining=%.2f of allocation %.2f, wallet=%.2f)",
                            bot.name, mode, sizing_capital, _quote, free_balance, pool, bot_total, wallet_total)
                    if _derivative:
                        # sizing_capital is margin; the order is leverage x bigger
                        trade_amount = engine._calculate_trade_amount(current_price, bot.settings, current_equity=sizing_capital, leverage=_leverage, side=side, spec=spec)
                    else:
                        trade_amount = engine._calculate_trade_amount(current_price, bot.settings, current_equity=sizing_capital, spec=spec)
                    if trade_amount is None:
                        logger.warning("Skipping %s for %s: no capital available to size trade", _label.lower(), symbol)
                        return None
                    if _entry_cfg.get("amount_type", "percentage") != "fixed":
                        # Percentage sizing must leave room for the taker fee
                        # (same clamp as the backtest/forward pool): 100% of
                        # the free balance is otherwise an InsufficientFunds
                        _fee_pct = engine._sim_frictions(bot.settings, side)[0]
                        trade_amount = min(trade_amount, spec.max_affordable_qty(sizing_capital, current_price, _leverage if _derivative else 1, _fee_pct))
                    trade_amount = _precise_amount(ccxt_inst, ccxt_symbol, trade_amount)
                    if trade_amount <= 0:
                        logger.warning("Trade amount rounded to zero for %s after precision, skipping", ccxt_symbol)
                        return None
                    min_violation = engine._below_market_minimum(ccxt_inst, ccxt_symbol, trade_amount, current_price)
                    if min_violation:
                        logger.warning("%s %s skipped for %s: %s", mode.upper(), _label, symbol, min_violation)
                        blb.push(bot.name, "WARN", f"{min_violation} — increase trade size")
                        return None
                    # Safety cap: clamp the order to max_order_value instead
                    # of dropping it — a silently skipped entry is a
                    # strategy that never trades, and the user can't see why
                    # `max_order_value` is a quote-currency notional (USD on BTC/USD:BTC, EUR on BTC/EUR)
                    max_order_quote = _num(bot.settings.get("max_order_value"), 0)
                    if max_order_quote > 0:
                        order_value_quote = spec.notional_quote(trade_amount, current_price)
                        if order_value_quote > max_order_quote:
                            capped = _precise_amount(ccxt_inst, ccxt_symbol, spec.qty_for_quote_notional(max_order_quote, current_price))
                            logger.warning("SAFETY: %s order %.2f %s exceeds max_order_value %.2f %s for %s — capped to %s", _label, order_value_quote, spec.quote, max_order_quote, spec.quote, symbol, capped)
                            blb.push(bot.name, "WARN", f"{_label} on {symbol} capped by max_order_value: {order_value_quote:,.0f} → {max_order_quote:,.0f} {spec.quote} (raise max_order_value or lower the entry amount to match the backtest)")
                            trade_amount = capped
                            min_violation = engine._below_market_minimum(ccxt_inst, ccxt_symbol, trade_amount, current_price)
                            if trade_amount <= 0 or min_violation:
                                blb.push(bot.name, "WARN", f"Capped order on {symbol} is below the exchange minimum ({min_violation or 'zero amount'}) — entry skipped")
                                return None
                    if _derivative:
                        # Leverage/margin mode must be confirmed on the exchange
                        # before the first contract is bought (raises on failure)
                        engine._ensure_leverage(ccxt_inst, api_key_record, ccxt_symbol, _leverage, bot.settings.get("margin_mode"), bot.name)
                    okx_order = _place_market_order(ccxt_inst, api_key_record, ccxt_symbol, _open_side, trade_amount, reduce_only=False, leverage=_leverage)
                    logger.info("%s %s response: id=%s status=%s filled=%s avg=%s fee=%s",
                        mode.upper(), _label, okx_order.get("id"), okx_order.get("status"),
                        okx_order.get("filled"), okx_order.get("average"), okx_order.get("fee"))
                    okx_order = engine._reconcile_order(ccxt_inst, okx_order, ccxt_symbol)
                    filled_qty = _filled_base(ccxt_inst, ccxt_symbol, okx_order)
                    if filled_qty <= 0 and okx_order.get("status") != "closed":
                        if engine._cancel_unfilled_order(ccxt_inst, okx_order.get("id"), ccxt_symbol):
                            logger.warning("%s %s unfilled (status=%s), canceled on exchange.", mode.upper(), _label, okx_order.get("status"))
                            db.add(Order(exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_open_side, order_type="market", price=current_price, amount=trade_amount, timestamp=latest_time, exchange_order_id=okx_order.get("id"), status="canceled", **_open_cols))
                            db.commit()
                            return None
                        # Cancel did not go through: the order may have filled
                        # after the last poll. Book the position conservatively
                        # at the requested amount so it stays tracked.
                        logger.error("%s %s state unknown for %s (id=%s) — booking position at requested amount, verify on the exchange", mode.upper(), _label, symbol, okx_order.get("id"))
                        blb.push(bot.name, "ERROR", f"{_label} order state unknown on {symbol} — position booked at requested amount/last price, verify manually on the exchange")
                    # Book the position for what actually filled, even
                    # when the exchange still reports the order as open
                    if filled_qty > 0:
                        trade_amount = filled_qty
                    actual_price = okx_order.get("average") or okx_order.get("price") or current_price
                    order_id = okx_order.get("id")
                    buy_fee = engine._fee_in_quote(okx_order.get("fee"), ccxt_symbol, actual_price, ccxt_inst)
                    _open_cols.update(_fee_cols(okx_order.get("fee"), _quote))
                    # A fee charged in base currency comes out of the bought
                    # amount itself; only the net amount is actually held
                    fee_info = okx_order.get("fee") or {}
                    base_ccy = base_of(ccxt_symbol)
                    if not _derivative and fee_info.get("currency") and str(fee_info["currency"]).upper() == base_ccy.upper():
                        try:
                            base_fee_cost = float(fee_info.get("cost") or 0)
                        except (TypeError, ValueError):
                            base_fee_cost = 0.0
                        if base_fee_cost > 0:
                            net_amount = float(ccxt_inst.amount_to_precision(ccxt_symbol, max(trade_amount - base_fee_cost, 0)))
                            if net_amount > 0:
                                trade_amount = net_amount
                    engine._balance_cache.pop((api_key_record.name, _quote), None)

                # Position created after successful exchange order
                open_position = Position(exchange=exchange, bot_name=bot.name, symbol=symbol, mode=mode, status="open", side=side, entry_price=actual_price, amount=trade_amount,
                                         cash_currency=_quote, contract_kind=spec.kind, contract_size=spec.contract_size)
                if _derivative:
                    open_position.market_type = _market_type
                    open_position.leverage = _leverage
                    # Funding is charged from the candle that opened the
                    # position (created_at is wall-clock, the candle may be older)
                    open_position.funding_until = latest_time
                    if mode in ["paper", "live"]:
                        open_position.contracts = spec.to_contracts(trade_amount)
                db.add(open_position)
                db.flush()

                db.add(Order(position_id=open_position.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_open_side, order_type="market", price=actual_price, amount=trade_amount, timestamp=latest_time, exchange_order_id=order_id, status="filled", fee=buy_fee, **_open_cols))
                # A real exchange fill must be persisted immediately — a later
                # rollback may not erase the record of it. Forward fills commit
                # here too: the console line below writes on its own connection
                # and would otherwise wait on this session's rows
                db.commit()
                logger.info("%s %s Filled @ %s", mode.upper(), _label, actual_price)
                blb.push(bot.name, "INFO", f"{mode.upper()} {_label} {symbol} @ {actual_price}")
                return open_position

            except ccxt.InsufficientFunds as e:
                logger.warning("%s %s rejected (insufficient funds): %s", mode.upper(), _label, e)
                blb.push(bot.name, "WARN", f"{mode.upper()} {_label} rejected: insufficient funds")
                db.add(Order(exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_open_side, order_type="market", price=current_price, amount=trade_amount, timestamp=latest_time, status="rejected", **_open_cols))
                if mode in ["paper", "live"]:
                    db.commit()
            except Exception as e:
                # The order never reached the exchange (or the exchange
                # refused it): "rejected", not "canceled" — a cancel
                # implies an order that existed
                logger.error("%s %s failed for %s: %s: %s", mode.upper(), _label, symbol, type(e).__name__, e, exc_info=True)
                blb.push(bot.name, "ERROR", f"{mode.upper()} {_label} {symbol} rejected — {type(e).__name__}: {str(e)[:200]}")
                db.add(Order(exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_open_side, order_type="market", price=current_price, amount=trade_amount, timestamp=latest_time, status="rejected", **_open_cols))
                if mode in ["paper", "live"]:
                    db.commit()
    return None


def process_tick(engine, exchange: str, symbol: str, timeframe: str, candle_ts=None):
    """Evaluate every matching bot on the candle that closed at `candle_ts`.
    The poller publishes one event per missed candle after a gap, and each
    must be traded and stop-checked in order — evaluating only the newest
    row would skip the entries and exits of everything in between. Without
    `candle_ts` the newest stored candle is used. Runs in a worker thread."""
    db = SessionLocal()
    try:
        active_bots = db.query(BotConfig).filter(BotConfig.is_active == True).all()

        # Batch-load all exchange keys once (avoids per-bot DB queries)
        all_key_names = {b.settings.get("api_key_name") for b in active_bots if b.settings.get("api_key_name")}
        key_records = {}
        if all_key_names:
            for kr in db.query(ExchangeKey).filter(ExchangeKey.name.in_(all_key_names)).all():
                key_records[kr.name] = kr

        matching_bots = []
        for b in active_bots:
            syms = b.settings.get("symbols", [])
            if not syms and b.settings.get("symbol"):
                syms = [b.settings.get("symbol")]
            # Match on exchange: derive bot's exchange from its key or data_exchange setting
            api_key_name = b.settings.get("api_key_name")
            bot_exchange = b.settings.get("data_exchange", "okx")
            if api_key_name:
                key_rec = key_records.get(api_key_name)
                if key_rec:
                    bot_exchange = key_rec.exchange or bot_exchange
            if symbol in syms and b.settings.get("timeframe") == timeframe and bot_exchange == exchange:
                matching_bots.append(b)

        if not matching_bots: return

        max_lookback = max([_int(b.settings.get("backtest_lookback"), 150) for b in matching_bots], default=150)

        query = db.query(Candle.id, Candle.timestamp, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume).filter(
            Candle.exchange == exchange, Candle.symbol == symbol, Candle.timeframe == timeframe
        )
        if candle_ts is not None:
            # Window ends at the candle being processed: the row under
            # evaluation is the last one and no newer candle can leak
            # into the indicators
            query = query.filter(Candle.timestamp <= _naive_utc(candle_ts))
        query = query.order_by(Candle.timestamp.desc()).limit(max_lookback).statement

        df = pd.read_sql(query, db.bind)

        if df.empty or len(df) < 20:
            return

        df = df.iloc[::-1].reset_index(drop=True)
        if candle_ts is not None and _naive_utc(df['timestamp'].iloc[-1]) != _naive_utc(candle_ts):
            logger.warning("Candle %s for %s/%s %s is not stored — evaluating the latest stored candle %s instead",
                           candle_ts, exchange, symbol, timeframe, df['timestamp'].iloc[-1])

        indicator_cache = {}  # fingerprint -> DataFrame with indicators computed

        # ── Batch pre-load: positions, orders counts (1 query each instead of N) ──
        matching_bot_names = [b.name for b in matching_bots if b.name not in engine._deleted_bots and b.name not in engine._backfilling_bots]

        # Pre-load ALL open positions for all matching bots in one query.
        # Not filtered on symbol: global max_positions scope must count
        # open positions across every whitelist pair.
        _all_open_positions = db.query(Position).options(
            selectinload(Position.orders)
        ).filter(
            Position.bot_name.in_(matching_bot_names),
            Position.status == "open"
        ).all() if matching_bot_names else []

        _positions_by_bot_mode = defaultdict(list)
        _funding_cache = {}  # (exchange, symbol) → stored funding events, one query per tick
        for p in _all_open_positions:
            _positions_by_bot_mode[(p.bot_name, p.mode)].append(p)

        # Pre-load cooldown buy counts for all bots in one query. The window
        # is counted in candles like the backtest (`index - idx <
        # cooldown_candles`): it starts at the N-th most recent stored candle
        # at or before the one being processed, not at wall-clock now minus
        # N x timeframe — a backlog replay or a gap would otherwise measure
        # a different window than the simulation.
        tf_seconds = _tf_seconds(timeframe)
        _latest_ts = _naive_utc(df['timestamp'].iloc[-1])

        def _cooldown_window_start(n_candles):
            if n_candles <= len(df):
                return _naive_utc(df['timestamp'].iloc[len(df) - n_candles])
            return _latest_ts - timedelta(seconds=(n_candles - 1) * tf_seconds)

        _cooldown_counts = {}
        cooldown_bots = [b for b in matching_bots if _int(b.settings.get("cooldown_trades"), 0) > 0 and _int(b.settings.get("cooldown_candles"), 0) > 0]
        if cooldown_bots:
            # Only executed buys in the bot's own mode count toward cooldown —
            # backtest history must not block live entries
            _bot_modes = {}
            for b in cooldown_bots:
                m = "forward_test"
                if b.settings.get("api_execution") and b.settings.get("api_key_name"):
                    kr = key_records.get(b.settings.get("api_key_name"))
                    if kr:
                        m = "paper" if kr.is_sandbox else "live"
                _bot_modes[b.name] = m
            _bot_windows = {
                b.name: _cooldown_window_start(_int(b.settings.get("cooldown_candles"), 0))
                for b in cooldown_bots
            }
            min_threshold = min(_bot_windows.values())
            # Opening orders: buys, plus the non-reduce-only sells that open a short
            _recent_buys = db.query(Order.bot_name, Order.mode, Order.timestamp).filter(
                Order.bot_name.in_([b.name for b in cooldown_bots]),
                Order.symbol == symbol,
                or_(Order.side == "buy", and_(Order.side == "sell", Order.market_type == "swap", Order.reduce_only == 0)),
                Order.status == "filled",
                Order.timestamp >= min_threshold,
                Order.timestamp <= _latest_ts,
            ).all()
            for _bn, _om, _ots in _recent_buys:
                if _om != _bot_modes.get(_bn):
                    continue
                if _naive_utc(_ots) >= _bot_windows[_bn]:
                    _cooldown_counts[_bn] = _cooldown_counts.get(_bn, 0) + 1

        # Collect all signal inserts for a single batch commit
        _pending_signals = []
        # Latest close per (exchange, symbol), shared by every bot's
        # mark-to-market risk check on this tick
        _last_close_cache = {}

        for bot in matching_bots:
            if bot.name in engine._deleted_bots or bot.name in engine._backfilling_bots:
                continue

            # One bot at a time: ticks for different symbols of the same
            # bot run in parallel worker threads, and the entry gate,
            # allocation pool and drawdown state are per bot
            with engine._bot_locks[bot.name]:
                # One failing bot must not kill the tick for the others:
                # their exits still have to be evaluated on this candle
                try:
                    # The batch snapshot above was taken before the lock: a
                    # tick for another symbol of this bot may have opened or
                    # closed a position meanwhile, so refresh this bot's slice
                    for _k in [k for k in _positions_by_bot_mode if k[0] == bot.name]:
                        del _positions_by_bot_mode[_k]
                    for _p in db.query(Position).options(selectinload(Position.orders)).filter(
                            Position.bot_name == bot.name, Position.status == "open").all():
                        _positions_by_bot_mode[(bot.name, _p.mode)].append(_p)
                    is_api_exec = bot.settings.get("api_execution", False)
                    has_key = bool(bot.settings.get("api_key_name"))
                    mode = "forward_test"
                    api_key_record = None
                    if is_api_exec and has_key:
                        api_key_record = key_records.get(bot.settings.get("api_key_name"))
                        if api_key_record:
                            mode = "paper" if api_key_record.is_sandbox else "live"
                        else:
                            logger.warning("Bot '%s': api_key_name='%s' not found. Running as forward_test.", bot.name, bot.settings.get("api_key_name"))
                            blb.push(bot.name, "WARN", f"API key '{bot.settings.get('api_key_name')}' not found, running as forward_test")

                    # Drawdown state is per mode group: a forward test never
                    # shares a curve with real money (risk.MODES_BY_GROUP)
                    _dd_group = risk.mode_group_for(mode)

                    # Risk guards on the realized live equity curve:
                    #  - max_capital_loss: loss of principal → close all + stop (any action)
                    #  - max_drawdown + close_all (default): close all + stop
                    #  - max_drawdown + block_entries: pause entries, keep exits,
                    #    resume below half the limit (hysteresis)
                    max_drawdown_pct = _num(bot.settings.get("max_drawdown"), 0)
                    max_capital_loss_pct = _num(bot.settings.get("max_capital_loss"), 0)
                    dd_action = bot.settings.get("drawdown_action", "close_all")
                    if max_drawdown_pct > 0 or max_capital_loss_pct > 0:
                        live_capital = _num(bot.settings.get("live_starting_capital"), 0) or _num(bot.settings.get("backtest_capital"), 1000)
                        _reset_raw = bot.settings.get("drawdown_peak_reset_at")
                        try:
                            _peak_reset_at = _naive_utc(datetime.fromisoformat(_reset_raw)) if _reset_raw else None
                        except (ValueError, TypeError):
                            _peak_reset_at = None
                        dd_state = engine._get_drawdown(bot.name, db, mode_group=_dd_group, starting_capital=live_capital, peak_reset_at=_peak_reset_at)
                        _open_real = [p for k, v in _positions_by_bot_mode.items() if k[0] == bot.name and k[1] in risk.MODES_BY_GROUP[_dd_group] for p in v]
                        # Mark-to-market like the backtest: open losses count
                        # before they are realized, so a stop fires on the
                        # same curve the backtest limit was tested on. Marked
                        # at the candle being processed, not the newest row,
                        # so a backlog replay measures the curve at its own time
                        _unrealized = engine._unrealized_pnl(db, _open_real, exchange, timeframe, _last_close_cache, candle_ts=_latest_ts)
                        dd_now, loss_now = engine._dd_now(dd_state, _unrealized)

                        stop_reason = None
                        _open_any = len(_open_real)
                        if max_capital_loss_pct > 0 and loss_now >= max_capital_loss_pct:
                            if dd_action == "block_entries" and _open_any > 0:
                                # Wind down: no new entries, exits keep running,
                                # the bot stops on the tick it turns flat. Loss of
                                # principal never recovers without trades, so
                                # unlike the drawdown block there is no resume.
                                if bot.name not in engine._entries_blocked:
                                    engine._entries_blocked.add(bot.name)
                                    logger.warning("Bot '%s' capital loss %.2f%% > %.2f%% — winding down (entries blocked, stops when flat)", bot.name, loss_now, max_capital_loss_pct)
                                    blb.push(bot.name, "WARN", f"Capital loss {loss_now:.1f}% > {max_capital_loss_pct:.0f}% — winding down: entries blocked, {_open_any} open position(s) keep their exits, bot stops when flat")
                            else:
                                blb.push(bot.name, "WARN", f"Capital loss {loss_now:.1f}% > {max_capital_loss_pct:.0f}% — bot stopped")
                                stop_reason = f"Capital loss {loss_now:.1f}% hit the {max_capital_loss_pct:.0f}% limit — " + ("bot flat, stopped" if _open_any == 0 else "positions closed")
                        elif max_drawdown_pct > 0 and dd_action != "block_entries" and dd_state["max_dd"] >= max_drawdown_pct:
                            blb.push(bot.name, "WARN", f"Max drawdown hit ({dd_state['max_dd']:.2f}% >= {max_drawdown_pct:.2f}%), auto-stopping")
                            stop_reason = f"Live drawdown {dd_state['max_dd']:.1f}% hit the {max_drawdown_pct:.0f}% limit — positions closed"

                        if stop_reason:
                            logger.warning("Bot '%s': %s", bot.name, stop_reason)
                            blb.push(bot.name, "WARN", "Closing all open positions before stopping — a stopped bot no longer manages SL/TP")
                            close_all_open_positions(engine, bot, db, key_records)
                            engine._engine_stop(bot, db, stop_reason)
                            engine._drawdown_cache.pop((bot.name, _dd_group), None)
                            engine._drawdown_cache.pop((bot.name, "backtest"), None)
                            engine._entries_blocked.discard(bot.name)
                            db.commit()
                            continue

                        _capital_wind_down = max_capital_loss_pct > 0 and loss_now >= max_capital_loss_pct
                        if max_drawdown_pct > 0 and dd_action == "block_entries" and not _capital_wind_down:
                            # Block on breach; release when drawdown recovers below
                            # half the limit, or once the bot has been flat for the
                            # cooldown — realized equity cannot recover without
                            # trades, so the peak is reset (persisted in settings so
                            # a restart doesn't re-block) and a new campaign starts.
                            # max_capital_loss stays the absolute stop across campaigns.
                            _cooldown_days = _num(bot.settings.get("drawdown_cooldown_days"), 7)
                            if bot.name not in engine._entries_blocked and dd_now >= max_drawdown_pct:
                                engine._entries_blocked.add(bot.name)
                                logger.warning("Bot '%s' drawdown %.2f%% > %.2f%% — new entries blocked", bot.name, dd_now, max_drawdown_pct)
                                blb.push(bot.name, "WARN", f"Max drawdown {dd_now:.1f}% > {max_drawdown_pct:.0f}% — new entries blocked (open positions keep their exits; resumes below {max_drawdown_pct * 0.5:.1f}% or after {_cooldown_days:.0f}d flat)")
                            elif bot.name in engine._entries_blocked:
                                if dd_now < max_drawdown_pct * 0.5:
                                    engine._entries_blocked.discard(bot.name)
                                    blb.push(bot.name, "INFO", f"Drawdown recovered to {dd_now:.1f}% — new entries allowed again")
                                elif _open_any == 0:
                                    _last_close = db.query(func.max(Position.closed_at)).filter(
                                        Position.bot_name == bot.name, Position.status == "closed",
                                        Position.mode.in_(risk.MODES_BY_GROUP[_dd_group])).scalar()
                                    _now_ts = _naive_utc(datetime.now(timezone.utc))
                                    _flat_secs = (_now_ts - _last_close).total_seconds() if _last_close is not None else float("inf")
                                    if _flat_secs >= _cooldown_days * 86400:
                                        engine._entries_blocked.discard(bot.name)
                                        bot.settings = {**bot.settings, "drawdown_peak_reset_at": _now_ts.isoformat()}
                                        engine._drawdown_cache.pop((bot.name, _dd_group), None)
                                        db.commit()
                                        blb.push(bot.name, "INFO", f"Flat for {_cooldown_days:.0f} days at {dd_now:.1f}% drawdown — peak reset, new entries allowed again (capital-loss guard remains)")
                    entries_blocked = bot.name in engine._entries_blocked

                    # Reuse indicator computation across bots with identical indicator configs
                    fp = _indicator_fingerprint(bot.settings)
                    if fp not in indicator_cache:
                        eval_tmp = NodeEvaluator(bot.settings)
                        eval_tmp.df = df.copy()
                        eval_tmp._calculate_indicators()
                        indicator_cache[fp] = eval_tmp.df

                    evaluator = NodeEvaluator(bot.settings)
                    evaluator.df = indicator_cache[fp]

                    latest_index = len(evaluator.df) - 1
                    latest_row = evaluator.df.iloc[latest_index]
                    latest_time = _naive_utc(latest_row['timestamp'])

                    current_price = float(latest_row['close'])
                    current_open = float(latest_row['open'])
                    current_high = float(latest_row['high'])
                    current_low = float(latest_row['low'])

                    current_atr = float(evaluator.df['atr'].iloc[latest_index]) if 'atr' in evaluator.df.columns and not pd.isna(evaluator.df['atr'].iloc[latest_index]) else 0.0

                    entry_series = evaluator.resolve_node(bot.settings.get("entry_node")) if bot.settings.get("entry_node") else pd.Series(False, index=evaluator.df.index)
                    exit_series = evaluator.resolve_node(bot.settings.get("exit_node")) if bot.settings.get("exit_node") else pd.Series(False, index=evaluator.df.index)

                    is_buy = bool(entry_series.iloc[-1])
                    is_sell = bool(exit_series.iloc[-1])
                    # Shorts (phase 3): only strategies with the nodes on a perpetual market
                    _short_node, _cover_node = bot.settings.get("short_node"), bot.settings.get("cover_node")
                    _shorts_enabled = bool(_short_node) and is_derivative(normalize(symbol))
                    is_short = bool(evaluator.resolve_node(_short_node).iloc[-1]) if _shorts_enabled else False
                    is_cover = bool(evaluator.resolve_node(_cover_node).iloc[-1]) if _shorts_enabled and _cover_node else False

                    tick_action = "BUY signal" if is_buy else ("SHORT signal" if is_short else ("SELL signal" if is_sell else ("COVER signal" if is_cover else "no signal")))
                    blb.push(bot.name, "INFO", f"Tick {symbol} {timeframe} | close {current_price} | {tick_action}")
                    try:
                        _tf_s = _tf_seconds(timeframe)
                        _next = datetime.fromtimestamp(((int(time.time()) // _tf_s) + 1) * _tf_s, tz=timezone.utc)
                        _prev_rt = engine.get_runtime(bot.name) or {}
                        engine.set_runtime(bot.name, "live", f"Last tick {symbol} @ {current_price:g} — {tick_action}",
                                         mode=_prev_rt.get("mode"), next_close=_next.isoformat(), last_tick_at=datetime.now(timezone.utc).isoformat())
                    except Exception as e:
                        logger.debug("runtime status update skipped for %s: %s", bot.name, e)

                    # Cache exchange instance per bot cycle to avoid repeated connections
                    _cached_ccxt = None
                    def get_ccxt():
                        nonlocal _cached_ccxt
                        if _cached_ccxt is None and api_key_record:
                            _cached_ccxt = engine._ccxt_for(api_key_record, symbol)
                            _cached_ccxt.load_markets()
                        return _cached_ccxt

                    max_pos = _int(bot.settings.get("max_positions"), 1)
                    scope = bot.settings.get("max_positions_scope", "per_pair")

                    # Use pre-loaded positions instead of per-bot DB query
                    bot_positions = [p for p in _positions_by_bot_mode.get((bot.name, mode), []) if p.symbol == symbol]
                    if scope == "per_pair":
                        open_count = len(bot_positions)
                    else:
                        # Global scope: all symbols, but only the bot's own mode — a
                        # position left open by the backtest must not take a live slot
                        open_count = len(_positions_by_bot_mode.get((bot.name, mode), []))

                    ccxt_symbol = normalize(symbol)
                    _close_cols = {"market_type": "swap", "reduce_only": 1} if is_derivative(ccxt_symbol) else {}
                    just_opened_ids = set()

                    # Cooldown check using pre-loaded counts
                    cooldown_trades = _int(bot.settings.get("cooldown_trades"), 0)
                    cooldown_candles = _int(bot.settings.get("cooldown_candles"), 0)

                    can_buy_cooldown = True
                    if cooldown_trades > 0 and cooldown_candles > 0:
                        recent_buys = _cooldown_counts.get(bot.name, 0)
                        if recent_buys >= cooldown_trades:
                            can_buy_cooldown = False

                    # Long and short never coexist on one pair: the conflicting
                    # signal is ignored (same rule as the backtest)
                    pair_side = (bot_positions[0].side or "long") if bot_positions else None
                    _try_buy = is_buy
                    if is_buy and pair_side == "short":
                        blb.push(bot.name, "INFO", f"BUY signal on {symbol} ignored while a short is open (long and short never coexist on a pair)")
                        _try_buy = False
                    opened = maybe_open_position(engine,
                        db, bot, exchange, symbol, mode, api_key_record, get_ccxt,
                        _try_buy, entries_blocked, bot_positions, open_count, max_pos, can_buy_cooldown,
                        current_price, latest_time)
                    if opened is None and is_short and not is_buy and _shorts_enabled:
                        if pair_side == "long":
                            blb.push(bot.name, "INFO", f"SHORT signal on {symbol} ignored while a long is open (long and short never coexist on a pair)")
                        else:
                            opened = maybe_open_position(engine,
                                db, bot, exchange, symbol, mode, api_key_record, get_ccxt,
                                is_short, entries_blocked, bot_positions, open_count, max_pos, can_buy_cooldown,
                                current_price, latest_time, side="short")
                    if opened is not None:
                        just_opened_ids.add(opened.id)

                    # Use pre-loaded positions (already includes orders via selectinload)
                    active_positions = bot_positions

                    # Forward test on a perpetual: funding on the positions
                    # that were open before this candle, the margin mode
                    # decides between a level per position and one account
                    _fwd_swap = mode == "forward_test" and is_derivative(ccxt_symbol)
                    _fwd_cross = _fwd_swap and (bot.settings.get("margin_mode") or DEFAULT_MARGIN_MODE) == "cross"
                    if _fwd_swap:
                        _apply_forward_funding(engine, db, bot, [p for p in active_positions if p.id not in just_opened_ids],
                                               exchange, ccxt_symbol, current_price, latest_time, _dd_group, _funding_cache)
                        _sym_tiers = tiers.load(db, exchange, ccxt_symbol)

                    for pos in active_positions:
                        if not bot.is_active: break
                        if pos.id in just_opened_ids: continue

                        # A short is closed by the cover signal with mirrored SL/TP,
                        # via reduce-only buys; its opening orders are sells
                        pos_side = pos.side or "long"
                        pos_short = pos_side == "short"
                        _pos_open_side, _pos_close_side = pnl.open_order_side(pos_side), pnl.close_order_side(pos_side)
                        _close_label = "COVER" if pos_short else "SELL"
                        pos_spec = _pos_spec(pos)
                        _pos_close_cols = {**_close_cols, "fee_currency": pos_spec.cash_currency}
                        exit_events = engine._check_exits(pos, current_price, current_high, current_low, is_cover if pos_short else is_sell, bot.settings, current_atr, row_open=current_open, side=pos_side)
                        # Forward test: the same price rule as the backtest.
                        # Exits that fill before the liquidation level are
                        # regular exits; whatever is still open afterwards
                        # (or would have filled beyond it) is liquidated.
                        _liq_price, _liq_hit = None, False
                        if _fwd_swap and not _fwd_cross:
                            _mmr = tiers.mmr_for(_sym_tiers, tiers.tier_size(pos_spec, pos.amount, pos.entry_price))
                            _liq_price = pos_spec.liquidation_price(pos_side, pos.entry_price, pos.leverage or 1, mmr=_mmr)
                            _liq_hit = pnl.liquidated(pos_side, _liq_price, current_high, current_low)
                            if _liq_hit:
                                exit_events = [ev for ev in exit_events if not pnl.liquidated(pos_side, _liq_price, ev['price'], ev['price'])]

                        # Track original amount for weighted profit_pct
                        # Use the sum of all opening orders as the original position size
                        pos_original_amount = _original_amount(pos, _pos_open_side)

                        for ev in exit_events:
                            if pos.amount <= 0: break

                            if ev.get('close_amount_type') == 'fixed':
                                close_qty = min(ev['qty_pct'], pos.amount)
                            else:
                                # Percentage of the original size (parity with the backtest)
                                close_qty = pos_original_amount * (ev['qty_pct'] / 100)
                            close_qty = min(close_qty, pos.amount)
                            if close_qty <= 0: continue

                            try:
                                actual_price = ev['price']
                                order_id = f"local_{int(latest_time.timestamp())}_{uuid.uuid4().hex[:8]}"

                                actual_fee = 0.0
                                if mode == "forward_test":
                                    # Backtest exit economics: slippage against
                                    # the fill, fee on the proceeds
                                    _, exit_fee_pct, _, exit_slip = engine._sim_frictions(bot.settings, pos_side)
                                    actual_price = ev['price'] * (1 + exit_slip) if pos_short else ev['price'] * (1 - exit_slip)
                                    actual_fee = pos_spec.fee_cash(close_qty, actual_price, exit_fee_pct)
                                elif mode in ["paper", "live"] and api_key_record:
                                    ccxt_inst = get_ccxt()
                                    close_qty = _precise_amount(ccxt_inst, ccxt_symbol, close_qty)
                                    if close_qty <= 0:
                                        logger.warning("Sell amount rounded to zero for %s after precision, skipping", ccxt_symbol)
                                        continue
                                    min_violation = engine._below_market_minimum(ccxt_inst, ccxt_symbol, close_qty, ev['price'])
                                    if min_violation:
                                        if engine._below_market_minimum(ccxt_inst, ccxt_symbol, pos.amount, ev['price']):
                                            # The whole remainder can never be sold on the
                                            # exchange; close the position administratively
                                            # instead of retrying a doomed sell forever
                                            logger.warning("%s position remainder on %s unsellable (%s) — closing administratively", mode.upper(), symbol, min_violation)
                                            blb.push(bot.name, "WARN", f"Position remainder on {symbol} below exchange minimum ({min_violation}) — closed administratively, dust remains on the exchange")
                                            pos.status = "closed"
                                            pos.closed_at = latest_time
                                            engine._update_drawdown(bot.name, _dd_group, 0.0)  # partial legs were booked as they closed
                                            with engine._position_states_lock:
                                                engine.position_states.pop(pos.id, None)
                                            db.commit()
                                            break
                                        logger.warning("%s %s skipped for %s: %s", mode.upper(), _close_label, symbol, min_violation)
                                        blb.push(bot.name, "WARN", f"{_close_label.capitalize()} on {symbol} skipped: {min_violation}")
                                        continue
                                    okx_order = _place_market_order(ccxt_inst, api_key_record, ccxt_symbol, _pos_close_side, close_qty, reduce_only=True, leverage=pos.leverage)
                                    logger.info("%s %s response: id=%s status=%s filled=%s avg=%s fee=%s",
                                        mode.upper(), _close_label, okx_order.get("id"), okx_order.get("status"),
                                        okx_order.get("filled"), okx_order.get("average"), okx_order.get("fee"))
                                    okx_order = engine._reconcile_order(ccxt_inst, okx_order, ccxt_symbol)
                                    filled_qty = _filled_base(ccxt_inst, ccxt_symbol, okx_order)
                                    if filled_qty <= 0 and okx_order.get("status") != "closed":
                                        if engine._cancel_unfilled_order(ccxt_inst, okx_order.get("id"), ccxt_symbol):
                                            logger.warning("%s %s unfilled (status=%s), canceled on exchange.", mode.upper(), _close_label, okx_order.get("status"))
                                            db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_pos_close_side, order_type="market", price=ev['price'], amount=close_qty, timestamp=latest_time, exchange_order_id=okx_order.get("id"), status="canceled", **_close_cols))
                                            db.commit()
                                            continue
                                        # Cancel did not go through: the sell may still fill on
                                        # the exchange. Keep the position amount untouched and
                                        # stop the bot — a second sell here could double-sell.
                                        logger.error("%s %s state unknown for %s (id=%s) — stopping bot '%s'", mode.upper(), _close_label, symbol, okx_order.get("id"), bot.name)
                                        blb.push(bot.name, "ERROR", f"Order state unknown on {symbol} — verify manually on the exchange before restarting")
                                        db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_pos_close_side, order_type="market", price=ev['price'], amount=close_qty, timestamp=latest_time, exchange_order_id=okx_order.get("id"), status="unknown", **_close_cols))
                                        engine._engine_stop(bot, db, f"{_close_label.capitalize()} order state unknown on {symbol} — verify on the exchange before restarting")
                                        db.commit()
                                        break
                                    # Book only what actually sold so a partial fill
                                    # reduces the position pro rata instead of being
                                    # retried for the full amount later
                                    if filled_qty > 0:
                                        close_qty = min(filled_qty, close_qty)
                                    actual_price = okx_order.get("average") or okx_order.get("price") or ev['price']
                                    order_id = okx_order.get("id")
                                    actual_fee = engine._fee_in_quote(okx_order.get("fee"), ccxt_symbol, actual_price, ccxt_inst)
                                    _pos_close_cols.update(_fee_cols(okx_order.get("fee"), pos_spec.cash_currency))
                                    engine._balance_cache.pop((api_key_record.name, pos_spec.cash_currency), None)

                                db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_pos_close_side, order_type="market", price=actual_price, amount=close_qty, timestamp=latest_time, exchange_order_id=order_id, status="filled", fee=actual_fee, **_pos_close_cols))

                                # Fee-adjusted P&L: subtract proportional entry fee + exit fee
                                total_buy_fees = _entry_fee_total(pos, _pos_open_side)
                                entry_fee_portion = total_buy_fees * (close_qty / pos_original_amount) if pos_original_amount > 0 else 0.0
                                realized_pnl = pnl.price_pnl(pos_side, pos.entry_price, actual_price, close_qty, spec=pos_spec) - entry_fee_portion - actual_fee
                                pos.profit_abs = (pos.profit_abs or 0.0) + realized_pnl

                                # Weighted profit_pct on the capital the leg had
                                # locked (margin + entry fee) — the backtest basis
                                if pos_original_amount > 0:
                                    entry_cost_for_qty = pnl.locked_capital(pos.entry_price, close_qty, pos.leverage or 1, spec=pos_spec) + entry_fee_portion
                                    portion_pct = (realized_pnl / entry_cost_for_qty) * 100 if entry_cost_for_qty > 0 else 0.0
                                    weight = close_qty / pos_original_amount
                                    pos.profit_pct = (pos.profit_pct or 0.0) + (portion_pct * weight)

                                with engine._position_states_lock:
                                    if pos.id in engine.position_states:
                                        engine.position_states[pos.id]['triggered_exits'].add(ev['id'])
                                        pos.triggered_exits = list(engine.position_states[pos.id]['triggered_exits'])

                                # Every closed leg moves the realized curve, partial or not
                                engine._update_drawdown(bot.name, _dd_group, realized_pnl)
                                if close_qty >= pos.amount - pnl.close_epsilon(pos_original_amount):
                                    pos.status = "closed"
                                    pos.closed_at = latest_time
                                    with engine._position_states_lock:
                                        engine.position_states.pop(pos.id, None)
                                else:
                                    pos.amount -= close_qty

                                # A real exchange fill must be persisted immediately —
                                # a later rollback may not erase the record of it;
                                # forward fills commit here too so the console line
                                # (own connection) never waits on this session
                                db.commit()

                                logger.info("%s %s (%s) Filled @ %s", mode.upper(), _close_label, ev['reason'], actual_price)
                                blb.push(bot.name, "INFO", f"{mode.upper()} {_close_label} [{ev['reason']}] {symbol} @ {actual_price}")
                            except Exception as e:
                                logger.error("%s %s failed: %s", mode.upper(), _close_label, e, exc_info=True)
                                blb.push(bot.name, "ERROR", f"{mode.upper()} {_close_label} failed: {e}")
                                db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side=_pos_close_side, order_type="market", price=current_price, amount=close_qty, timestamp=latest_time, status="rejected", **_close_cols))
                                if mode in ["paper", "live"]:
                                    db.commit()
                                    # A rejected reduce-only close on a perpetual
                                    # usually means the exchange no longer holds
                                    # the position (liquidated or closed by
                                    # hand): ask, and stop retrying forever
                                    if is_derivative(ccxt_symbol) and api_key_record and pos.status == "open":
                                        try:
                                            if broker.exchange_position_gone(get_ccxt(), ccxt_symbol, pos_side):
                                                _book_liquidation(engine, db, bot, pos, current_price, latest_time, _dd_group,
                                                                  "exchange reports no open position after a rejected close — booked as liquidation, check the exchange")
                                                db.commit()
                                                break
                                        except Exception as probe_exc:
                                            logger.warning("Could not verify %s position on the exchange after a rejected close: %s", symbol, probe_exc)

                        if _liq_hit and pos.status == "open":
                            _book_liquidation(engine, db, bot, pos, _liq_price, latest_time, _dd_group,
                                              f"candle range reached the liquidation level {_liq_price:g} at {pos.leverage or 1:g}x")

                    # Cross margin: the whole account after this candle's
                    # exits (every symbol of the bot); a breach liquidates
                    # all open forward positions and the free cash is lost
                    # with them (same booking as the backtest)
                    if _fwd_cross:
                        _all_open = [p for p in _positions_by_bot_mode.get((bot.name, mode), []) if p.status == "open" and p.id not in just_opened_ids]
                        _breach = _forward_cross_breached(db, engine, bot, _all_open, exchange, timeframe, ccxt_symbol,
                                                          current_high, current_low, latest_time, _last_close_cache)
                        if _breach is not None:
                            _marks, _cash_lost = _breach
                            _total_margin = sum(_pos_spec(p).margin(p.amount or 0.0, p.entry_price or 0.0, max(float(p.leverage or 1), 1.0)) for p in _all_open)
                            for p in _all_open:
                                _m_i = _pos_spec(p).margin(p.amount or 0.0, p.entry_price or 0.0, max(float(p.leverage or 1), 1.0))
                                _extra = _cash_lost * _m_i / _total_margin if _total_margin > 0 else 0.0
                                _book_liquidation(engine, db, bot, p, _marks.get(p.id, p.entry_price), latest_time, _dd_group,
                                                  f"cross-margin account liquidation on {symbol} — equity fell to the maintenance margin, wallet lost", extra_loss=_extra)
                            blb.push(bot.name, "WARN", f"FORWARD_TEST cross-margin liquidation: {len(_all_open)} position(s) closed, {_cash_lost:,.2f} free cash lost")

                    standard_cols = ['id', 'timestamp', 'open', 'high', 'low', 'close', 'volume', 'atr']
                    indicators = { col: float(latest_row[col]) for col in evaluator.df.columns if col not in standard_cols and not pd.isna(latest_row[col]) }

                    if indicators:
                        action_str = "buy" if is_buy else ("short" if is_short else ("sell" if is_sell else ("cover" if is_cover else "neutral")))
                        live_ts = latest_time
                        if hasattr(live_ts, 'to_pydatetime'):
                            live_ts = live_ts.to_pydatetime()
                        _pending_signals.append({"cid": int(latest_row['id']), "sym": symbol, "ts": str(live_ts), "bn": bot.name, "nm": "STRATEGY_TICK", "act": action_str, "ed": json.dumps(indicators)})
                    # Persist this bot's forward fills before the next bot
                    # runs: a failure there rolls back only its own work
                    db.commit()
                except Exception as _bot_exc:
                    logger.error("Bot '%s' failed on %s %s @ %s: %s", bot.name, symbol, timeframe, candle_ts, _bot_exc, exc_info=True)
                    blb.push(bot.name, "ERROR", f"Tick {symbol} {timeframe} failed: {_bot_exc}")
                    try:
                        db.rollback()
                    except Exception:
                        pass

        # ── Single batch commit for all signals and position/order changes ──
        if _pending_signals:
            for sig_params in _pending_signals:
                db.execute(
                    text("INSERT OR IGNORE INTO signals (candle_id, symbol, timestamp, bot_name, name, action, extra_data) VALUES (:cid, :sym, :ts, :bn, :nm, :act, :ed)"),
                    sig_params
                )
        db.commit()

    except Exception as e:
        logger.error("Error executing live bot strategy: %s", e, exc_info=True)
        db.rollback()
    finally:
        db.close()
