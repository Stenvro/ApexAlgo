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
from sqlalchemy import text, func
from sqlalchemy.orm import selectinload
from backend.core.database import SessionLocal
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.orders import Order
from backend.models.positions import Position
from backend.models.exchange_keys import ExchangeKey
from backend.engine.evaluator import NodeEvaluator
from backend.engine.sizing import _indicator_fingerprint, _num, _int, _tf_seconds, _naive_utc
from backend.core import bot_log_buffer as blb

logger = logging.getLogger("apexalgo.bot_manager")


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
        ccxt_symbol = pos.symbol.replace('-', '/').upper()

        if pos.mode in ("paper", "live"):
            if ccxt_inst is None:
                logger.error("Cannot close %s position on %s for '%s': no exchange client. Position left open.", pos.mode, pos.symbol, bot.name)
                blb.push(bot.name, "ERROR", f"Could not close {pos.mode} position on {pos.symbol}: exchange unavailable — close it manually!")
                continue
            try:
                sell_qty = float(ccxt_inst.amount_to_precision(ccxt_symbol, close_qty))
                if sell_qty <= 0:
                    continue
                ex_order = ccxt_inst.create_market_sell_order(ccxt_symbol, sell_qty)
                ex_order = engine._reconcile_order(ccxt_inst, ex_order, ccxt_symbol)
                filled_qty = float(ex_order.get("filled") or 0)
                if filled_qty <= 0 and ex_order.get("status") != "closed":
                    db.add(Order(position_id=pos.id, exchange=pos.exchange, bot_name=bot.name, mode=pos.mode, symbol=pos.symbol, side="sell", order_type="market", price=close_price, amount=sell_qty, timestamp=now_ts, exchange_order_id=ex_order.get("id"), status="canceled"))
                    db.commit()
                    blb.push(bot.name, "ERROR", f"Forced close on {pos.symbol} did not fill; position left open — close it manually!")
                    continue
                close_qty = filled_qty if filled_qty > 0 else sell_qty
                actual_price = ex_order.get("average") or ex_order.get("price") or close_price
                order_id = ex_order.get("id") or order_id
                actual_fee = engine._fee_in_quote(ex_order.get("fee"), ccxt_symbol, actual_price)
            except Exception as exc:
                logger.error("Forced close failed for '%s' on %s: %s", bot.name, pos.symbol, exc, exc_info=True)
                blb.push(bot.name, "ERROR", f"Forced close failed on {pos.symbol}: {exc} — close it manually!")
                continue

        realized_pnl = (actual_price - pos.entry_price) * close_qty - actual_fee
        pos.profit_abs = (pos.profit_abs or 0.0) + realized_pnl
        if pos.entry_price and pos.amount:
            pos.profit_pct = (pos.profit_pct or 0.0) + ((actual_price - pos.entry_price) / pos.entry_price) * 100 * (close_qty / pos.amount)

        db.add(Order(position_id=pos.id, exchange=pos.exchange, bot_name=bot.name, mode=pos.mode, symbol=pos.symbol, side="sell", order_type="market", price=actual_price, amount=close_qty, timestamp=now_ts, exchange_order_id=order_id, status="filled", fee=actual_fee))

        if close_qty >= pos.amount - 0.00001:
            pos.status = "closed"
            pos.closed_at = now_ts
            with engine._position_states_lock:
                engine.position_states.pop(pos.id, None)
        else:
            pos.amount -= close_qty
            blb.push(bot.name, "WARN", f"Partial forced close on {pos.symbol}: {close_qty} sold, {pos.amount} still open — close it manually!")

        if pos.mode in ("paper", "live"):
            db.commit()

        logger.info("Forced close [%s] %s: %s @ %s (PnL %+.2f)", pos.mode, pos.symbol, close_qty, actual_price, realized_pnl)
        blb.push(bot.name, "INFO", f"Forced close [{pos.mode}] {pos.symbol}: {close_qty} @ {actual_price} (PnL {realized_pnl:+.2f})")


def maybe_open_position(engine, db, bot, exchange, symbol, mode, api_key_record, get_ccxt,
                         is_buy, entries_blocked, bot_positions, open_count, max_pos, can_buy_cooldown,
                         current_price, latest_time):
    """Entry leg of one live tick. Returns the freshly opened Position, or
    None when no entry was made (no signal, blocked, skipped, or failed).
    Kept separate from the exit loop on purpose: bailing out of the entry
    must never skip the SL/TP evaluation of the positions already open."""
    ccxt_symbol = symbol.replace('-', '/').upper()
    if is_buy and entries_blocked:
        blb.push(bot.name, "INFO", f"BUY signal on {symbol} skipped — entries blocked by max drawdown")
    elif is_buy and open_count >= max_pos:
        # Pyramiding cap, same rule as the backtest (per_pair: this symbol,
        # global: the whole portfolio)
        blb.push(bot.name, "INFO", f"BUY signal on {symbol} skipped — max_positions ({max_pos}) reached")
    elif is_buy and can_buy_cooldown:
        trade_amount = engine._calculate_trade_amount(current_price, bot.settings)
        if trade_amount is None:
            logger.warning("Skipping buy for %s: invalid trade amount", symbol)
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
                    _quote = ccxt_symbol.split('/')[-1]
                    pool = engine._forward_pool(db, bot, _quote)
                    if pool <= 0:
                        blb.push(bot.name, "WARN", f"BUY signal on {symbol} skipped — forward-test pool depleted ({pool:,.2f} {_quote})")
                        return None
                    entry_fee_pct, _, entry_slip, _ = engine._sim_frictions(bot.settings)
                    trade_amount = engine._calculate_trade_amount(current_price, bot.settings, current_equity=pool)
                    if trade_amount is None:
                        return None
                    actual_price = current_price * (1 + entry_slip)
                    if bot.settings.get("trade_settings", {}).get("entry", {}).get("amount_type", "percentage") != "fixed":
                        trade_amount = min(trade_amount, pool / (actual_price * (1 + entry_fee_pct)))
                    if trade_amount <= 0 or actual_price * trade_amount * (1 + entry_fee_pct) > pool + 1e-9:
                        blb.push(bot.name, "WARN", f"BUY signal on {symbol} skipped — entry does not fit the forward-test pool ({pool:,.2f} {_quote})")
                        return None
                    buy_fee = actual_price * trade_amount * entry_fee_pct
                elif mode in ["paper", "live"] and api_key_record:
                    ccxt_inst = get_ccxt()

                    # A restart replays the last candle: never place a second
                    # BUY for a candle that already produced one
                    existing_buy = db.query(Order.id).filter(
                        Order.bot_name == bot.name, Order.symbol == symbol,
                        Order.mode == mode, Order.side == "buy",
                        Order.timestamp == latest_time,
                    ).first()
                    if existing_buy:
                        logger.info("%s BUY for %s @ %s already recorded, skipping duplicate entry", mode.upper(), symbol, latest_time)
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
                        logger.info("Bot '%s': sandbox balance unavailable, sizing paper entry from backtest capital $%.2f", bot.name, sizing_capital)
                    else:
                        _quote = ccxt_symbol.split('/')[-1]
                        pool, wallet_total, bot_total = engine._live_allocation(db, bot, _quote, free_balance)
                        if pool <= 0:
                            blb.push(bot.name, "WARN", f"BUY signal on {symbol} skipped — allocation fully deployed ({bot_total:,.0f} {_quote} = {_num(bot.settings.get('live_allocation_pct'), 100):.0f}% of wallet {wallet_total:,.0f})")
                            return None
                        sizing_capital = min(free_balance, pool)
                        logger.info("Bot '%s': sizing %s entry from $%.2f (free=$%.2f, pool remaining=$%.2f of allocation $%.2f, wallet=$%.2f)",
                            bot.name, mode, sizing_capital, free_balance, pool, bot_total, wallet_total)
                    trade_amount = engine._calculate_trade_amount(current_price, bot.settings, current_equity=sizing_capital)
                    if trade_amount is None:
                        logger.warning("Skipping buy for %s: no capital available to size trade", symbol)
                        return None
                    trade_amount = float(ccxt_inst.amount_to_precision(ccxt_symbol, trade_amount))
                    if trade_amount <= 0:
                        logger.warning("Trade amount rounded to zero for %s after precision, skipping", ccxt_symbol)
                        return None
                    min_violation = engine._below_market_minimum(ccxt_inst, ccxt_symbol, trade_amount, current_price)
                    if min_violation:
                        logger.warning("%s BUY skipped for %s: %s", mode.upper(), symbol, min_violation)
                        blb.push(bot.name, "WARN", f"{min_violation} — increase trade size")
                        return None
                    # Safety cap: clamp the order to max_order_value instead
                    # of dropping it — a silently skipped entry is a
                    # strategy that never trades, and the user can't see why
                    max_order_usd = _num(bot.settings.get("max_order_value"), 0)
                    if max_order_usd > 0:
                        order_value_usd = trade_amount * current_price
                        if order_value_usd > max_order_usd:
                            capped = float(ccxt_inst.amount_to_precision(ccxt_symbol, max_order_usd / current_price))
                            logger.warning("SAFETY: BUY order $%.2f exceeds max_order_value $%.2f for %s — capped to %s", order_value_usd, max_order_usd, symbol, capped)
                            blb.push(bot.name, "WARN", f"BUY on {symbol} capped by max_order_value: {order_value_usd:,.0f} → {max_order_usd:,.0f} {ccxt_symbol.split('/')[-1]} (raise max_order_value or lower the entry amount to match the backtest)")
                            trade_amount = capped
                            min_violation = engine._below_market_minimum(ccxt_inst, ccxt_symbol, trade_amount, current_price)
                            if trade_amount <= 0 or min_violation:
                                blb.push(bot.name, "WARN", f"Capped order on {symbol} is below the exchange minimum ({min_violation or 'zero amount'}) — entry skipped")
                                return None
                    okx_order = ccxt_inst.create_market_buy_order(ccxt_symbol, trade_amount)
                    logger.info("%s BUY response: id=%s status=%s filled=%s avg=%s fee=%s",
                        mode.upper(), okx_order.get("id"), okx_order.get("status"),
                        okx_order.get("filled"), okx_order.get("average"), okx_order.get("fee"))
                    okx_order = engine._reconcile_order(ccxt_inst, okx_order, ccxt_symbol)
                    filled_qty = float(okx_order.get("filled") or 0)
                    if filled_qty <= 0 and okx_order.get("status") != "closed":
                        if engine._cancel_unfilled_order(ccxt_inst, okx_order.get("id"), ccxt_symbol):
                            logger.warning("%s BUY unfilled (status=%s), canceled on exchange.", mode.upper(), okx_order.get("status"))
                            db.add(Order(exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="buy", order_type="market", price=current_price, amount=trade_amount, timestamp=latest_time, exchange_order_id=okx_order.get("id"), status="canceled"))
                            db.commit()
                            return None
                        # Cancel did not go through: the order may have filled
                        # after the last poll. Book the position conservatively
                        # at the requested amount so it stays tracked.
                        logger.error("%s BUY state unknown for %s (id=%s) — booking position at requested amount, verify on the exchange", mode.upper(), symbol, okx_order.get("id"))
                        blb.push(bot.name, "ERROR", f"BUY order state unknown on {symbol} — position booked at requested amount/last price, verify manually on the exchange")
                    # Book the position for what actually filled, even
                    # when the exchange still reports the order as open
                    if filled_qty > 0:
                        trade_amount = filled_qty
                    actual_price = okx_order.get("average") or okx_order.get("price") or current_price
                    order_id = okx_order.get("id")
                    buy_fee = engine._fee_in_quote(okx_order.get("fee"), ccxt_symbol, actual_price)
                    # A fee charged in base currency comes out of the bought
                    # amount itself; only the net amount is actually held
                    fee_info = okx_order.get("fee") or {}
                    base_ccy = ccxt_symbol.split('/')[0]
                    if fee_info.get("currency") and str(fee_info["currency"]).upper() == base_ccy.upper():
                        try:
                            base_fee_cost = float(fee_info.get("cost") or 0)
                        except (TypeError, ValueError):
                            base_fee_cost = 0.0
                        if base_fee_cost > 0:
                            net_amount = float(ccxt_inst.amount_to_precision(ccxt_symbol, max(trade_amount - base_fee_cost, 0)))
                            if net_amount > 0:
                                trade_amount = net_amount
                    engine._balance_cache.pop((api_key_record.name, ccxt_symbol.split('/')[-1]), None)

                # Position created after successful exchange order
                open_position = Position(exchange=exchange, bot_name=bot.name, symbol=symbol, mode=mode, status="open", side="long", entry_price=actual_price, amount=trade_amount)
                db.add(open_position)
                db.flush()

                db.add(Order(position_id=open_position.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="buy", order_type="market", price=actual_price, amount=trade_amount, timestamp=latest_time, exchange_order_id=order_id, status="filled", fee=buy_fee))
                if mode in ["paper", "live"]:
                    # A real exchange fill must be persisted immediately —
                    # a later rollback may not erase the record of it
                    db.commit()
                logger.info("%s BUY Filled @ %s", mode.upper(), actual_price)
                blb.push(bot.name, "INFO", f"{mode.upper()} BUY {symbol} @ {actual_price}")
                return open_position

            except ccxt.InsufficientFunds as e:
                logger.warning("%s BUY rejected (insufficient funds): %s", mode.upper(), e)
                blb.push(bot.name, "WARN", f"{mode.upper()} BUY rejected: insufficient funds")
                db.add(Order(exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="buy", order_type="market", price=current_price, amount=trade_amount, timestamp=latest_time, status="rejected"))
                if mode in ["paper", "live"]:
                    db.commit()
            except Exception as e:
                logger.error("%s BUY failed: %s", mode.upper(), e, exc_info=True)
                blb.push(bot.name, "ERROR", f"{mode.upper()} BUY failed: {e}")
                db.add(Order(exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="buy", order_type="market", price=current_price, amount=trade_amount, timestamp=latest_time, status="canceled"))
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

        max_lookback = max([int(b.settings.get("backtest_lookback", 150)) for b in matching_bots], default=150)

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
        for p in _all_open_positions:
            _positions_by_bot_mode[(p.bot_name, p.mode)].append(p)

        # Pre-load cooldown buy counts for all bots in one query
        tf_seconds = 60
        if timeframe.endswith('m'): tf_seconds = int(timeframe[:-1]) * 60
        elif timeframe.endswith('h'): tf_seconds = int(timeframe[:-1]) * 3600
        elif timeframe.endswith('d'): tf_seconds = int(timeframe[:-1]) * 86400

        _cooldown_counts = {}
        cooldown_bots = [b for b in matching_bots if int(b.settings.get("cooldown_trades", 0)) > 0 and int(b.settings.get("cooldown_candles", 0)) > 0]
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
            now_utc = datetime.now(timezone.utc)
            _bot_windows = {
                b.name: now_utc - timedelta(seconds=int(b.settings.get("cooldown_candles", 0)) * tf_seconds)
                for b in cooldown_bots
            }
            max_cooldown_candles = max(int(b.settings.get("cooldown_candles", 0)) for b in cooldown_bots)
            min_threshold = _naive_utc(now_utc - timedelta(seconds=max_cooldown_candles * tf_seconds))
            _recent_buys = db.query(Order.bot_name, Order.mode, Order.timestamp).filter(
                Order.bot_name.in_([b.name for b in cooldown_bots]),
                Order.symbol == symbol,
                Order.side == "buy",
                Order.status == "filled",
                Order.timestamp > min_threshold
            ).all()
            for _bn, _om, _ots in _recent_buys:
                if _om != _bot_modes.get(_bn):
                    continue
                if _ots.tzinfo is None:
                    _ots = _ots.replace(tzinfo=timezone.utc)
                if _ots > _bot_windows[_bn]:
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
                # The batch snapshot above was taken before the lock: a
                # tick for another symbol of this bot may have opened or
                # closed a position meanwhile, so refresh this bot's slice
                for _k in [k for k in _positions_by_bot_mode if k[0] == bot.name]:
                    del _positions_by_bot_mode[_k]
                for _p in db.query(Position).options(selectinload(Position.orders)).filter(
                        Position.bot_name == bot.name, Position.status == "open").all():
                    _positions_by_bot_mode[(bot.name, _p.mode)].append(_p)
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
                    dd_state = engine._get_drawdown(bot.name, db, mode_group="live", starting_capital=live_capital, peak_reset_at=_peak_reset_at)
                    _open_real = [p for k, v in _positions_by_bot_mode.items() if k[0] == bot.name and k[1] != "backtest" for p in v]
                    # Mark-to-market like the backtest: open losses count
                    # before they are realized, so a stop fires on the
                    # same curve the backtest limit was tested on
                    _unrealized = engine._unrealized_pnl(db, _open_real, exchange, timeframe, _last_close_cache)
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
                        engine._drawdown_cache.pop((bot.name, "live"), None)
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
                                    Position.mode.in_(["forward_test", "paper", "live"])).scalar()
                                _now_ts = _naive_utc(datetime.now(timezone.utc))
                                _flat_secs = (_now_ts - _last_close).total_seconds() if _last_close is not None else float("inf")
                                if _flat_secs >= _cooldown_days * 86400:
                                    engine._entries_blocked.discard(bot.name)
                                    bot.settings = {**bot.settings, "drawdown_peak_reset_at": _now_ts.isoformat()}
                                    engine._drawdown_cache.pop((bot.name, "live"), None)
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

                tick_action = "BUY signal" if is_buy else ("SELL signal" if is_sell else "no signal")
                blb.push(bot.name, "INFO", f"Tick {symbol} {timeframe} | close {current_price} | {tick_action}")
                try:
                    _tf_s = _tf_seconds(timeframe)
                    _next = datetime.fromtimestamp(((int(time.time()) // _tf_s) + 1) * _tf_s, tz=timezone.utc)
                    _prev_rt = engine.get_runtime(bot.name) or {}
                    engine.set_runtime(bot.name, "live", f"Last tick {symbol} @ {current_price:g} — {tick_action}",
                                     mode=_prev_rt.get("mode"), next_close=_next.isoformat(), last_tick_at=datetime.now(timezone.utc).isoformat())
                except Exception as e:
                    logger.debug("runtime status update skipped for %s: %s", bot.name, e)

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

                # Cache exchange instance per bot cycle to avoid repeated connections
                _cached_ccxt = None
                def get_ccxt():
                    nonlocal _cached_ccxt
                    if _cached_ccxt is None and api_key_record:
                        _cached_ccxt = engine._get_ccxt_instance(api_key_record)
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

                ccxt_symbol = symbol.replace('-', '/').upper()
                just_opened_ids = set()

                # Cooldown check using pre-loaded counts
                cooldown_trades = _int(bot.settings.get("cooldown_trades"), 0)
                cooldown_candles = _int(bot.settings.get("cooldown_candles"), 0)

                can_buy_cooldown = True
                if cooldown_trades > 0 and cooldown_candles > 0:
                    recent_buys = _cooldown_counts.get(bot.name, 0)
                    if recent_buys >= cooldown_trades:
                        can_buy_cooldown = False

                opened = maybe_open_position(engine,
                    db, bot, exchange, symbol, mode, api_key_record, get_ccxt,
                    is_buy, entries_blocked, bot_positions, open_count, max_pos, can_buy_cooldown,
                    current_price, latest_time)
                if opened is not None:
                    just_opened_ids.add(opened.id)

                # Use pre-loaded positions (already includes orders via selectinload)
                active_positions = bot_positions

                for pos in active_positions:
                    if not bot.is_active: break
                    if pos.id in just_opened_ids: continue

                    exit_events = engine._check_exits(pos, current_price, current_high, current_low, is_sell, bot.settings, current_atr, row_open=current_open)

                    # Track original amount for weighted profit_pct
                    # Use the sum of all buy orders as the original position size
                    pos_original_amount = sum(
                        o.amount for o in (pos.orders or []) if o.side == "buy" and o.status == "filled"
                    ) or pos.amount

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
                                _, exit_fee_pct, _, exit_slip = engine._sim_frictions(bot.settings)
                                actual_price = ev['price'] * (1 - exit_slip)
                                actual_fee = actual_price * close_qty * exit_fee_pct
                            elif mode in ["paper", "live"] and api_key_record:
                                ccxt_inst = get_ccxt()
                                close_qty = float(ccxt_inst.amount_to_precision(ccxt_symbol, close_qty))
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
                                        engine._update_drawdown(bot.name, "live", pos.profit_abs)
                                        with engine._position_states_lock:
                                            engine.position_states.pop(pos.id, None)
                                        db.commit()
                                        break
                                    logger.warning("%s SELL skipped for %s: %s", mode.upper(), symbol, min_violation)
                                    blb.push(bot.name, "WARN", f"Sell on {symbol} skipped: {min_violation}")
                                    continue
                                okx_order = ccxt_inst.create_market_sell_order(ccxt_symbol, close_qty)
                                logger.info("%s SELL response: id=%s status=%s filled=%s avg=%s fee=%s",
                                    mode.upper(), okx_order.get("id"), okx_order.get("status"),
                                    okx_order.get("filled"), okx_order.get("average"), okx_order.get("fee"))
                                okx_order = engine._reconcile_order(ccxt_inst, okx_order, ccxt_symbol)
                                filled_qty = float(okx_order.get("filled") or 0)
                                if filled_qty <= 0 and okx_order.get("status") != "closed":
                                    if engine._cancel_unfilled_order(ccxt_inst, okx_order.get("id"), ccxt_symbol):
                                        logger.warning("%s SELL unfilled (status=%s), canceled on exchange.", mode.upper(), okx_order.get("status"))
                                        db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="sell", order_type="market", price=ev['price'], amount=close_qty, timestamp=latest_time, exchange_order_id=okx_order.get("id"), status="canceled"))
                                        db.commit()
                                        continue
                                    # Cancel did not go through: the sell may still fill on
                                    # the exchange. Keep the position amount untouched and
                                    # stop the bot — a second sell here could double-sell.
                                    logger.error("%s SELL state unknown for %s (id=%s) — stopping bot '%s'", mode.upper(), symbol, okx_order.get("id"), bot.name)
                                    blb.push(bot.name, "ERROR", f"Order state unknown on {symbol} — verify manually on the exchange before restarting")
                                    db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="sell", order_type="market", price=ev['price'], amount=close_qty, timestamp=latest_time, exchange_order_id=okx_order.get("id"), status="unknown"))
                                    engine._engine_stop(bot, db, f"Sell order state unknown on {symbol} — verify on the exchange before restarting")
                                    db.commit()
                                    break
                                # Book only what actually sold so a partial fill
                                # reduces the position pro rata instead of being
                                # retried for the full amount later
                                if filled_qty > 0:
                                    close_qty = min(filled_qty, close_qty)
                                actual_price = okx_order.get("average") or okx_order.get("price") or ev['price']
                                order_id = okx_order.get("id")
                                actual_fee = engine._fee_in_quote(okx_order.get("fee"), ccxt_symbol, actual_price)
                                engine._balance_cache.pop((api_key_record.name, ccxt_symbol.split('/')[-1]), None)

                            db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="sell", order_type="market", price=actual_price, amount=close_qty, timestamp=latest_time, exchange_order_id=order_id, status="filled", fee=actual_fee))

                            # Fee-adjusted P&L: subtract proportional entry fee + exit fee
                            total_buy_fees = sum((o.fee or 0.0) for o in (pos.orders or []) if o.side == "buy" and o.status == "filled")
                            entry_fee_portion = total_buy_fees * (close_qty / pos_original_amount) if pos_original_amount > 0 else 0.0
                            realized_pnl = (actual_price - pos.entry_price) * close_qty - entry_fee_portion - actual_fee
                            pos.profit_abs = (pos.profit_abs or 0.0) + realized_pnl

                            # Weighted profit_pct: fee-adjusted, based on portion of original position
                            if pos_original_amount > 0:
                                entry_cost_for_qty = pos.entry_price * close_qty + entry_fee_portion
                                portion_pct = (realized_pnl / entry_cost_for_qty) * 100 if entry_cost_for_qty > 0 else 0.0
                                weight = close_qty / pos_original_amount
                                pos.profit_pct = (pos.profit_pct or 0.0) + (portion_pct * weight)

                            with engine._position_states_lock:
                                if pos.id in engine.position_states:
                                    engine.position_states[pos.id]['triggered_exits'].add(ev['id'])
                                    pos.triggered_exits = list(engine.position_states[pos.id]['triggered_exits'])

                            if close_qty >= pos.amount - 0.00001:
                                pos.status = "closed"
                                pos.closed_at = latest_time
                                engine._update_drawdown(bot.name, "live", pos.profit_abs)
                                with engine._position_states_lock:
                                    engine.position_states.pop(pos.id, None)
                            else:
                                pos.amount -= close_qty

                            if mode in ["paper", "live"]:
                                # A real exchange fill must be persisted immediately —
                                # a later rollback may not erase the record of it
                                db.commit()

                            logger.info("%s SELL (%s) Filled @ %s", mode.upper(), ev['reason'], actual_price)
                            blb.push(bot.name, "INFO", f"{mode.upper()} SELL [{ev['reason']}] {symbol} @ {actual_price}")
                        except Exception as e:
                            logger.error("%s SELL failed: %s", mode.upper(), e, exc_info=True)
                            blb.push(bot.name, "ERROR", f"{mode.upper()} SELL failed: {e}")
                            db.add(Order(position_id=pos.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="sell", order_type="market", price=current_price, amount=close_qty, timestamp=latest_time, status="rejected"))
                            if mode in ["paper", "live"]:
                                db.commit()

                standard_cols = ['id', 'timestamp', 'open', 'high', 'low', 'close', 'volume', 'atr']
                indicators = { col: float(latest_row[col]) for col in evaluator.df.columns if col not in standard_cols and not pd.isna(latest_row[col]) }

                if indicators:
                    action_str = "buy" if is_buy else ("sell" if is_sell else "neutral")
                    live_ts = latest_time
                    if hasattr(live_ts, 'to_pydatetime'):
                        live_ts = live_ts.to_pydatetime()
                    _pending_signals.append({"cid": int(latest_row['id']), "sym": symbol, "ts": str(live_ts), "bn": bot.name, "nm": "STRATEGY_TICK", "act": action_str, "ed": json.dumps(indicators)})

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
