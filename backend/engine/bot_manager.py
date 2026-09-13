import asyncio
import json
import logging
import threading
import time
import uuid
from hashlib import md5
import pandas as pd
import ccxt
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from sqlalchemy import text, func
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from backend.core.database import SessionLocal
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.signals import Signal
from backend.models.orders import Order
from backend.models.positions import Position
from backend.models.exchange_keys import ExchangeKey
from backend.engine.evaluator import NodeEvaluator
from backend.core.events import event_bus
from backend.core.encryption import decrypt_data
from backend.core.exchange_registry import build_exchange_from_key, get_exchange_timeframes
from backend.core import bot_log_buffer as blb

logger = logging.getLogger("apexalgo.bot_manager")

VALID_EXIT_TYPES = {'percentage', 'trailing', 'atr', 'fixed'}

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


class BotManager:
    def __init__(self):
        self.running = False
        self.position_states = {}
        # position_states is mutated from backfill threads and _process_bots
        # worker threads, so a threading lock (not asyncio) guards it
        self._position_states_lock = threading.Lock()
        self._drawdown_cache = {}  # (bot_name, mode_group) -> {peak_pnl, running_pnl, max_dd}
        # Bots whose max_drawdown breached with drawdown_action=block_entries:
        # no new entries until drawdown recovers below half the limit. Purely
        # in-memory — re-derived from the drawdown cache on the first tick
        # after a restart, so it survives without persistence.
        self._entries_blocked = set()
        self._deleted_bots = set()  # bot names pending cleanup, skip in processing
        self._backfilling_bots = set()  # bot names currently in backfill, skip in live processing
        self._candle_locks = defaultdict(asyncio.Lock)  # (exchange, symbol, timeframe) -> serializer
        self._processed_candles = {}  # (exchange, symbol, timeframe) -> last processed candle ts
        self._balance_cache = {}  # (key_name, quote_ccy) -> (fetched_at, free_balance)
        self._bg_tasks = set()  # strong refs so fire-and-forget tasks are not GC'd mid-flight
        self._runtime = {}  # bot_name -> {phase, detail, progress, mode, updated_at, ...}
        self._runtime_lock = threading.Lock()
        self._run_tokens = {}  # bot_id -> token of the current startup thread

    # ── Runtime status (what the UI shows while a bot is starting/running) ──
    def set_runtime(self, bot_name: str, phase: str, detail: str = "", progress=None, **extra):
        with self._runtime_lock:
            self._runtime[bot_name] = {
                "phase": phase,
                "detail": detail,
                "progress": progress,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **extra,
            }

    def clear_runtime(self, bot_name: str):
        with self._runtime_lock:
            self._runtime.pop(bot_name, None)

    def get_runtime(self, bot_name: str):
        with self._runtime_lock:
            rt = self._runtime.get(bot_name)
            return dict(rt) if rt else None

    def rename_runtime(self, old_name: str, new_name: str):
        with self._runtime_lock:
            if old_name in self._runtime:
                self._runtime[new_name] = self._runtime.pop(old_name)

    def _engine_stop(self, bot, db, reason: str):
        """Stop a bot from inside the engine and remember why, so the card can
        show the reason after the fact (a user stop clears it)."""
        bot.is_active = False
        try:
            bot.settings = {**(bot.settings or {}), "last_stop_reason": reason}
            flag_modified(bot, "settings")
        except Exception:
            pass
        self.set_runtime(bot.name, "halted", reason)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _get_drawdown(self, bot_name, db, mode_group="live", starting_capital=1000.0, peak_reset_at=None):
        """Return cached drawdown state, lazy-initializing from DB on first access.
        mode_group: "backtest" for backtest-only, "live" for forward_test/paper/live.
        starting_capital: wallet size used as equity base for percentage calculation.
        peak_reset_at: naive-UTC datetime; closes before it only move the equity
        base (block_entries cooldown started a new drawdown campaign there)."""
        cache_key = (bot_name, mode_group)
        if cache_key not in self._drawdown_cache:
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
            self._drawdown_cache[cache_key] = {"starting_capital": starting_capital, "peak_equity": peak_equity, "running_pnl": running, "max_dd": dd}
        return self._drawdown_cache[cache_key]

    def _update_drawdown(self, bot_name, mode_group, profit_abs):
        """Incrementally update drawdown cache when a position closes."""
        cache_key = (bot_name, mode_group)
        if cache_key in self._drawdown_cache:
            s = self._drawdown_cache[cache_key]
            s["running_pnl"] += (profit_abs or 0)
            equity = s["starting_capital"] + s["running_pnl"]
            s["peak_equity"] = max(s["peak_equity"], equity)
            if s["peak_equity"] > 0:
                s["max_dd"] = max(s["max_dd"], ((s["peak_equity"] - equity) / s["peak_equity"]) * 100)

    @staticmethod
    def _dd_now(state):
        """Current (not historical-max) drawdown % and capital-loss % from a
        drawdown-cache state. Current drawdown recovers as equity climbs back,
        which is what the block_entries hysteresis needs."""
        equity = state["starting_capital"] + state["running_pnl"]
        peak = state["peak_equity"]
        dd = ((peak - equity) / peak) * 100 if peak > 0 else 0.0
        start = state["starting_capital"]
        loss = ((start - equity) / start) * 100 if start > 0 else 0.0
        return max(dd, 0.0), max(loss, 0.0)

    async def start(self):
        self.running = True
        logger.info("Bot Manager started. Engine is fully operational.")

        self._spawn(self._startup_backfill())
        self._spawn(self._listen_for_bot_starts())

        queue = event_bus.subscribe("CANDLE_CLOSED")
        while self.running:
            try:
                event_data = await asyncio.wait_for(queue.get(), timeout=1.0)
                exchange = event_data.get("exchange", "okx")
                symbol = event_data["symbol"]
                timeframe = event_data["timeframe"]
                candle_ts = event_data.get("timestamp")
                self._spawn(self._handle_candle_close(exchange, symbol, timeframe, candle_ts))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _handle_candle_close(self, exchange: str, symbol: str, timeframe: str, candle_ts):
        """Serialize processing per subscription and skip re-published candles.
        Poll tasks re-emit after a reconnect, so the same candle can arrive twice;
        without this, two concurrent runs could place duplicate live orders."""
        key = (exchange, symbol, timeframe)
        async with self._candle_locks[key]:
            if candle_ts is not None:
                prev = self._processed_candles.get(key)
                if prev is not None and candle_ts <= prev:
                    return
            await self._process_bots(exchange, symbol, timeframe)
            if candle_ts is not None:
                self._processed_candles[key] = candle_ts

    def _get_ccxt_instance(self, api_key_record: ExchangeKey):
        return build_exchange_from_key(api_key_record)

    def _reconcile_order(self, ccxt_inst, order, ccxt_symbol, attempts=5, delay=1.0):
        """Market orders often report status open/None on creation even though they
        fill (near-)immediately; poll the exchange until a terminal state is known.
        Returns the freshest order dict available."""
        for _ in range(attempts):
            status = order.get("status")
            if status in ("canceled", "rejected", "expired"):
                break
            if status == "closed" and order.get("filled") is not None:
                break
            order_id = order.get("id")
            if not order_id:
                break
            time.sleep(delay)
            try:
                refreshed = ccxt_inst.fetch_order(order_id, ccxt_symbol)
            except Exception as exc:
                logger.warning("fetch_order %s failed: %s", order_id, exc)
                continue
            if refreshed:
                merged = {k: v for k, v in refreshed.items() if v is not None}
                order = {**order, **merged}
        return order

    def _cancel_unfilled_order(self, ccxt_inst, order_id, ccxt_symbol):
        """Try to cancel an order whose fill state could not be confirmed.
        Returns True when the cancel definitively succeeded (the order did not
        fill), False when the order may still have filled — e.g. cancel raises
        'order not found' or 'already filled' — so the caller must treat the
        order state as unknown instead of silently booking it as canceled."""
        if not order_id:
            # Order never got an exchange id, so nothing on the exchange can fill
            return True
        try:
            ccxt_inst.cancel_order(order_id, ccxt_symbol)
            return True
        except Exception as exc:
            logger.warning("cancel_order %s failed: %s", order_id, exc)
            return False

    @staticmethod
    def _below_market_minimum(ccxt_inst, ccxt_symbol, amount, price):
        """Return a human-readable violation string when an order would fall
        below the exchange's minimum amount/cost limits, else None. Missing
        limit metadata is treated as no restriction."""
        try:
            limits = (ccxt_inst.market(ccxt_symbol) or {}).get("limits") or {}
            min_amount = (limits.get("amount") or {}).get("min")
            min_cost = (limits.get("cost") or {}).get("min")
            if min_amount is not None and amount < float(min_amount):
                return f"amount {amount} below exchange minimum {float(min_amount)}"
            if min_cost is not None and price:
                order_value = amount * float(price)
                if order_value < float(min_cost):
                    return f"order ${order_value:.2f} below exchange minimum ${float(min_cost):.2f}"
        except Exception:
            return None
        return None

    @staticmethod
    def _fee_in_quote(fee_info, ccxt_symbol, price):
        """CCXT fee cost can be denominated in base currency (typical for buys);
        convert to quote so it can be netted against PnL."""
        if not fee_info:
            return 0.0
        try:
            cost = float(fee_info.get("cost", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        currency = fee_info.get("currency")
        base = ccxt_symbol.split('/')[0] if '/' in ccxt_symbol else None
        if currency and base and currency.upper() == base.upper() and price:
            return cost * float(price)
        return cost

    @staticmethod
    def _deployed_capital(db, bot_names, quote):
        """Quote-currency cost (entry price x amount) of the open paper/live
        positions of the given bots in pairs quoted in `quote`."""
        if not bot_names:
            return 0.0
        rows = db.query(Position.entry_price, Position.amount, Position.symbol).filter(
            Position.bot_name.in_(list(bot_names)), Position.status == "open",
            Position.mode.in_(["paper", "live"])).all()
        return sum((r[0] or 0.0) * (r[1] or 0.0) for r in rows if (r[2] or "").replace('-', '/').upper().endswith('/' + quote))

    def _live_allocation(self, db, bot, quote, free_balance):
        """Capital this bot may still deploy: its share (live_allocation_pct) of
        the wallet's quote equity — free balance plus what every bot on the same
        key already has in open positions — minus its own open positions.
        Returns (pool_remaining, wallet_total, bot_total)."""
        pct = min(max(_num(bot.settings.get("live_allocation_pct"), 100), 0.0), 100.0)
        key_name = bot.settings.get("api_key_name")
        peers = [b.name for b in db.query(BotConfig).all() if (b.settings or {}).get("api_key_name") == key_name]
        if bot.name not in peers:
            peers.append(bot.name)
        deployed_key = self._deployed_capital(db, peers, quote)
        deployed_bot = self._deployed_capital(db, [bot.name], quote)
        wallet_total = free_balance + deployed_key
        bot_total = wallet_total * pct / 100.0
        return max(bot_total - deployed_bot, 0.0), wallet_total, bot_total

    def _get_live_capital(self, ccxt_inst, api_key_record, ccxt_symbol, bot_name, ttl=30):
        """Free quote-currency balance on the exchange, cached briefly to spare
        rate limits. Returns None when the balance cannot be determined so the
        caller can fall back to the configured capital."""
        quote = ccxt_symbol.split('/')[-1]
        cache_key = (api_key_record.name, quote)
        now = time.monotonic()
        cached = self._balance_cache.get(cache_key)
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        try:
            balance = ccxt_inst.fetch_balance()
            free = None
            if isinstance(balance.get(quote), dict):
                free = balance[quote].get("free")
            if free is None:
                free = (balance.get("free") or {}).get(quote)
            if free is not None:
                free = float(free)
                self._balance_cache[cache_key] = (now, free)
                return free
            logger.warning("No %s balance found for key '%s'", quote, api_key_record.name)
            blb.push(bot_name, "WARN", f"Could not read {quote} balance from exchange")
        except Exception as exc:
            logger.warning("fetch_balance failed for key '%s': %s", api_key_record.name, exc)
            blb.push(bot_name, "WARN", f"Balance fetch failed: {exc}")
        return None

    def _close_all_open_positions(self, bot, db, key_records):
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
                ccxt_inst = self._get_ccxt_instance(api_key_record)
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
                    ex_order = self._reconcile_order(ccxt_inst, ex_order, ccxt_symbol)
                    filled_qty = float(ex_order.get("filled") or 0)
                    if filled_qty <= 0 and ex_order.get("status") != "closed":
                        db.add(Order(position_id=pos.id, exchange=pos.exchange, bot_name=bot.name, mode=pos.mode, symbol=pos.symbol, side="sell", order_type="market", price=close_price, amount=sell_qty, timestamp=now_ts, exchange_order_id=ex_order.get("id"), status="canceled"))
                        db.commit()
                        blb.push(bot.name, "ERROR", f"Forced close on {pos.symbol} did not fill; position left open — close it manually!")
                        continue
                    close_qty = filled_qty if filled_qty > 0 else sell_qty
                    actual_price = ex_order.get("average") or ex_order.get("price") or close_price
                    order_id = ex_order.get("id") or order_id
                    actual_fee = self._fee_in_quote(ex_order.get("fee"), ccxt_symbol, actual_price)
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
                with self._position_states_lock:
                    self.position_states.pop(pos.id, None)
            else:
                pos.amount -= close_qty
                blb.push(bot.name, "WARN", f"Partial forced close on {pos.symbol}: {close_qty} sold, {pos.amount} still open — close it manually!")

            if pos.mode in ("paper", "live"):
                db.commit()

            logger.info("Forced close [%s] %s: %s @ %s (PnL %+.2f)", pos.mode, pos.symbol, close_qty, actual_price, realized_pnl)
            blb.push(bot.name, "INFO", f"Forced close [{pos.mode}] {pos.symbol}: {close_qty} @ {actual_price} (PnL {realized_pnl:+.2f})")

    def _calculate_trade_amount(self, current_price, bot_settings, current_equity=None):
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

    def _check_exits(self, open_position, row_close, row_high, row_low, is_sell_signal, bot_settings, current_atr=0.0, row_open=None):
        trade_settings = bot_settings.get("trade_settings", {})
        entry_settings = trade_settings.get("entry", {})
        events = []
        if row_open is None:
            row_open = row_close

        with self._position_states_lock:
            state = self.position_states.get(open_position.id)
            if not state or state.get('entry_price') != open_position.entry_price:
                # Restore persisted state from DB, or initialize fresh
                persisted_highest = open_position.highest_price or open_position.entry_price
                persisted_exits = set(open_position.triggered_exits or [])
                state = {
                    'entry_price': open_position.entry_price,
                    'highest_price': persisted_highest,
                    'triggered_exits': persisted_exits
                }
                self.position_states[open_position.id] = state
            # Trailing levels anchor to the peak reached BEFORE this candle; the
            # current candle's high is folded in afterwards so one candle cannot
            # both raise the trail and trigger it against its own low.
            prev_highest = state['highest_price']
            triggered_exits = set(state['triggered_exits'])

        sl_hit = False
        for i, sl in enumerate(entry_settings.get("stop_losses", [])):
            sl_id = f"sl_{i}"
            if sl_id in triggered_exits: continue

            try:
                sl_val = float(sl.get('value', 0))
            except (ValueError, TypeError):
                continue

            if sl_val <= 0: continue

            sl_type = sl.get('type', '')
            if sl_type not in VALID_EXIT_TYPES:
                logger.warning("Invalid stop_loss type '%s' for sl_%d, skipping", sl_type, i)
                continue

            if sl_type == 'percentage':
                trigger_price = open_position.entry_price * (1 - (sl_val/100))
            elif sl_type == 'trailing':
                trigger_price = prev_highest * (1 - (sl_val/100))
            elif sl_type == 'atr':
                if not (current_atr > 0): continue
                trigger_price = prev_highest - (sl_val * current_atr)
            else:
                trigger_price = sl_val

            if row_low <= trigger_price:
                sl_close_type = sl.get('close_amount_type', 'percentage')
                sl_close_val = float(sl.get('close_amount_value', 100))
                events.append({
                    'qty_pct': sl_close_val,
                    'close_amount_type': sl_close_type,
                    'reason': "stop_loss",
                    # A gap below the trigger fills at the open, not the trigger
                    'price': min(trigger_price, row_open),
                    'id': sl_id
                })
                sl_hit = True

        if not sl_hit:
            tps = []
            for i, tp in enumerate(entry_settings.get("take_profits", [])):
                tp_id = f"tp_{i}"
                if tp_id in triggered_exits: continue

                try:
                    tp_val = float(tp.get('value', 0))
                except (ValueError, TypeError):
                    continue

                if tp_val <= 0: continue

                tp_type = tp.get('type', '')
                if tp_type not in VALID_EXIT_TYPES:
                    logger.warning("Invalid take_profit type '%s' for tp_%d, skipping", tp_type, i)
                    continue

                tp_close_type = tp.get('close_amount_type', 'percentage')
                tp_close_val = float(tp.get('close_amount_value', 100))

                if tp_type == 'percentage':
                    t_price = open_position.entry_price * (1 + (tp_val/100))
                    if row_high >= t_price:
                        # A gap above the target fills at the (better) open price
                        tps.append({'id': tp_id, 'price': max(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
                elif tp_type == 'trailing':
                    # Trailing TP: price must first rise above entry by tp_val%, then
                    # we close when price drops tp_val% from the highest price reached.
                    activation_price = open_position.entry_price * (1 + (tp_val / 100))
                    if prev_highest >= activation_price:
                        # Once activated, trail below the peak
                        t_price = prev_highest * (1 - (tp_val / 100))
                        if row_low <= t_price:
                            tps.append({'id': tp_id, 'price': min(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
                elif tp_type == 'atr':
                    if not (current_atr > 0): continue
                    t_price = prev_highest - (tp_val * current_atr)
                    if row_low <= t_price:
                        tps.append({'id': tp_id, 'price': min(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
                else:
                    t_price = tp_val
                    if row_high >= t_price:
                        tps.append({'id': tp_id, 'price': max(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})

            tps = sorted(tps, key=lambda x: x['price'], reverse=True)

            for tp in tps:
                events.append({
                    'qty_pct': tp['pct'],
                    'close_amount_type': tp.get('close_amount_type', 'percentage'),
                    'reason': "take_profit",
                    'price': tp['price'],
                    'id': tp['id']
                })

        if not events and is_sell_signal:
            exit_settings = trade_settings.get("exit", {})
            pct_to_close = _num(exit_settings.get('amount_value'), 100) if exit_settings.get('amount_type') == 'percentage' else 100
            events.append({
                'qty_pct': pct_to_close,
                'reason': "strategy",
                'price': row_close,
                'id': 'strategy_sell'
            })

        with self._position_states_lock:
            state['highest_price'] = max(state['highest_price'], row_high)
            # Persist state back to DB for crash recovery
            open_position.highest_price = state['highest_price']

        return events

    async def _startup_backfill(self):
        def get_active_bot_ids():
            db = SessionLocal()
            try:
                active_bots = db.query(BotConfig).filter(BotConfig.is_active == True).all()
                return [bot.id for bot in active_bots]
            finally:
                db.close()

        bot_ids = await asyncio.to_thread(get_active_bot_ids)
        for bot_id in bot_ids:
            self._spawn(self._run_backfill_safely(bot_id))

    async def _listen_for_bot_starts(self):
        queue = event_bus.subscribe("BOT_STATE_CHANGED")
        while self.running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                if event["action"] == "started":
                    self._spawn(self._run_backfill_safely(event["bot_id"]))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _run_backfill_safely(self, bot_id: int):
        try:
            await asyncio.to_thread(self._execute_sync_backfill, bot_id)
        except Exception as e:
            logger.error("Error during thread backfill for bot_id=%s: %s", bot_id, e, exc_info=True)

    def _flush_backtest_data(self, db, bot_name: str):
        """Remove a bot's previous backtest results (signals + backtest-mode
        positions/orders) so a new run simulates the full window cleanly.
        Live/paper/forward positions are untouched. Chunked deletes keep the
        write-lock short next to concurrent backfill commits."""
        from sqlalchemy import text as _text
        try:
            for table, where in (
                ("signals", "bot_name = :bn"),
                ("orders", "bot_name = :bn AND mode = 'backtest'"),
                ("positions", "bot_name = :bn AND mode = 'backtest'"),
            ):
                while True:
                    res = db.execute(_text(
                        f"DELETE FROM {table} WHERE rowid IN "
                        f"(SELECT rowid FROM {table} WHERE {where} LIMIT 20000)"
                    ), {"bn": bot_name})
                    db.commit()
                    if res.rowcount == 0:
                        break
            self._drawdown_cache.pop((bot_name, "backtest"), None)
        except Exception as exc:
            db.rollback()
            logger.warning("Could not flush previous backtest data for '%s': %s", bot_name, exc)

    def _still_active(self, bot_id: int, token=None) -> bool:
        """Fresh-session check so a user stop during backfill/backtest is
        honoured. A stop+start (restart) issues a new run token, so the
        superseded thread also bails out instead of running twice."""
        if token is not None and self._run_tokens.get(bot_id) != token:
            return False
        _db = SessionLocal()
        try:
            row = _db.query(BotConfig.is_active).filter(BotConfig.id == bot_id).first()
            return bool(row and row[0])
        finally:
            _db.close()

    class _StoppedByUser(Exception):
        pass

    def _execute_sync_backfill(self, bot_id: int):
        db = SessionLocal()
        _log_name = f"bot_id={bot_id}"
        _run_token = None
        try:
            bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
            if not bot or not bot.is_active: return
            _log_name = bot.name
            _run_token = object()
            self._run_tokens[bot_id] = _run_token
            self._backfilling_bots.add(bot.name)
            self.set_runtime(bot.name, "starting", "Preparing engine…")

            is_api_exec = bot.settings.get("api_execution", False)
            has_key = bool(bot.settings.get("api_key_name"))

            live_mode = "forward_test"
            exchange_name = bot.settings.get("data_exchange", "okx")
            if is_api_exec and has_key:
                api_key = db.query(ExchangeKey).filter(ExchangeKey.name == bot.settings.get("api_key_name")).first()
                if api_key:
                    live_mode = "paper" if api_key.is_sandbox else "live"
                    exchange_name = api_key.exchange or exchange_name
                else:
                    logger.warning("Bot '%s': api_key_name='%s' not found in database. Falling back to forward_test mode.", bot.name, bot.settings.get("api_key_name"))
                    blb.push(bot.name, "WARN", f"API key '{bot.settings.get('api_key_name')}' not found, running as forward_test")

            timeframe = bot.settings.get("timeframe")
            exit_node = bot.settings.get("exit_node")
            run_backtest = bot.settings.get("backtest_on_start", False)
            lookback_limit = _int(bot.settings.get("backtest_lookback"), 150)

            if run_backtest:
                # A backtest is deterministic, so always simulate the whole
                # window from scratch. Stitching a new run onto leftovers of a
                # previous one (different data range or exchange) produces a
                # patchwork of trades with a double capital start.
                self._flush_backtest_data(db, bot.name)

            symbols = bot.settings.get("symbols", [])
            if not symbols and bot.settings.get("symbol"):
                symbols = [bot.settings.get("symbol")]

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
            bt_trade_settings = bot.settings.get("trade_settings", {})
            bt_entry_fee = _num(bt_trade_settings.get("entry", {}).get("fee"), 0) / 100
            raw_exit_fee = bt_trade_settings.get("exit", {}).get("fee")
            bt_exit_fee = _num(raw_exit_fee, bt_entry_fee * 100) / 100 if raw_exit_fee not in (None, "") else bt_entry_fee
            bt_entry_slippage = _num(bt_trade_settings.get("entry", {}).get("slippage"), 0) / 100
            bt_exit_slippage = _num(bt_trade_settings.get("exit", {}).get("slippage"), 0) / 100

            cooldown_trades = _int(bot.settings.get("cooldown_trades"), 0)
            cooldown_candles = _int(bot.settings.get("cooldown_candles"), 0)

            # Per-symbol data prep first (indicators stay per symbol); execution
            # then runs over one merged timeline so all symbols contend for the
            # shared capital pool in chronological order.
            sym_contexts = []
            empty_symbols = []

            for symbol in symbols:
                blb.push(bot.name, "INFO", f"Starting: {symbol} | {timeframe} | {live_mode} | lookback={lookback_limit}")
                tf_seconds = 60
                if timeframe.endswith('m'): tf_seconds = int(timeframe[:-1]) * 60
                elif timeframe.endswith('h'): tf_seconds = int(timeframe[:-1]) * 3600
                elif timeframe.endswith('d'): tf_seconds = int(timeframe[:-1]) * 86400

                # Use fresh sessions for all polling queries. The main `db` session starts an
                # implicit SQLite transaction on its first read (line above), so any subsequent
                # reads on it see a stale snapshot and will never reflect candles written by
                # the streamer in a separate session. Fresh sessions start new transactions
                # that see all committed data.
                def _count_candles():
                    _db = SessionLocal()
                    try:
                        return _db.query(Candle.id).filter(Candle.exchange == exchange_name, Candle.symbol == symbol, Candle.timeframe == timeframe).count()
                    finally:
                        _db.close()

                def _latest_candle_ts():
                    _db = SessionLocal()
                    try:
                        return _db.query(Candle.timestamp).filter(
                            Candle.exchange == exchange_name, Candle.symbol == symbol, Candle.timeframe == timeframe
                        ).order_by(Candle.timestamp.desc()).first()
                    finally:
                        _db.close()

                initial_count = _count_candles()
                sym_idx = symbols.index(symbol) + 1
                if initial_count < lookback_limit:
                    logger.info("Waiting for candle data: %s (%d/%d candles)...", symbol, initial_count, lookback_limit)
                    blb.push(bot.name, "INFO", f"Fetching historical data: {symbol} ({initial_count}/{lookback_limit} candles)...")
                    self.set_runtime(bot.name, "fetching", f"{symbol} · {initial_count}/{lookback_limit} candles",
                                     {"done": initial_count, "total": lookback_limit}, symbol=symbol, symbol_index=sym_idx, symbol_count=len(symbols))

                    max_wait = 300  # 5 minutes max
                    waited = 0
                    stable_checks = 0
                    last_count = initial_count
                    last_log_count = initial_count

                    while waited < max_wait:
                        time.sleep(2)
                        waited += 2
                        if not self._still_active(bot_id, _run_token):
                            raise self._StoppedByUser()
                        current_count = _count_candles()
                        if current_count != last_count:
                            self.set_runtime(bot.name, "fetching", f"{symbol} · {current_count}/{lookback_limit} candles",
                                             {"done": min(current_count, lookback_limit), "total": lookback_limit}, symbol=symbol, symbol_index=sym_idx, symbol_count=len(symbols))

                        # Log progress when count changes significantly
                        if current_count - last_log_count >= 100:
                            blb.push(bot.name, "INFO", f"Fetching historical data: {symbol} ({current_count}/{lookback_limit} candles)...")
                            last_log_count = current_count

                        if current_count >= lookback_limit:
                            break

                        if current_count == last_count:
                            # Zero candles is never "done": with several bots
                            # starting at once the poller may not have reached
                            # this subscription yet — keep waiting for data
                            # instead of concluding the backfill finished.
                            if current_count > 0:
                                stable_checks += 1
                                if stable_checks >= 5:  # 10 seconds of no change — backfill done
                                    break
                        else:
                            stable_checks = 0

                        last_count = current_count

                final_count = _count_candles()
                logger.info("Data available for %s: %d candles.", symbol, final_count)
                if final_count == 0:
                    # Distinguish a genuinely unsupported timeframe from a
                    # backfill that simply hasn't delivered (busy poller,
                    # rate limit) so the user isn't sent down the wrong path
                    empty_symbols.append(symbol)
                    try:
                        supported = get_exchange_timeframes(exchange_name)
                    except Exception:
                        supported = None
                    if supported and timeframe not in supported:
                        blb.push(bot.name, "ERROR", f"No candle data for {symbol}: {exchange_name} does not support the '{timeframe}' timeframe.")
                    else:
                        blb.push(bot.name, "ERROR", f"No candle data received for {symbol} on {exchange_name} ({timeframe}) — the exchange may be busy or rate-limited. Bot will stop; try starting it again.")
                elif final_count < lookback_limit:
                    blb.push(bot.name, "INFO", f"Historical data ready: {symbol} ({final_count}/{lookback_limit} requested)")
                else:
                    blb.push(bot.name, "INFO", f"Historical data ready: {symbol} ({final_count} candles)")

                # Only wait for fresh candles if this bot does live/paper execution
                if live_mode in ("paper", "live"):
                    for _ in range(20):
                        latest_candle = _latest_candle_ts()
                        if latest_candle:
                            candle_ts = latest_candle[0]
                            if candle_ts.tzinfo is None: candle_ts = candle_ts.replace(tzinfo=timezone.utc)
                            diff_seconds = datetime.now(timezone.utc).timestamp() - candle_ts.timestamp()
                            if diff_seconds <= (tf_seconds * 2): break
                        time.sleep(1)

                # Fresh session for the candle read as well — same stale-snapshot reason.
                candle_db = SessionLocal()
                try:
                    query = candle_db.query(Candle.id, Candle.timestamp, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume).filter(
                        Candle.exchange == exchange_name, Candle.symbol == symbol, Candle.timeframe == timeframe
                    ).order_by(Candle.timestamp.desc()).limit(lookback_limit).statement
                    df = pd.read_sql(query, candle_db.bind)
                finally:
                    candle_db.close()

                if df.empty or len(df) < 20:
                    logger.info("Skipping backfill for %s: insufficient data (%d candles, minimum 20 required).", symbol, len(df))
                    blb.push(bot.name, "WARN", f"Skipping backtest: {symbol} — only {len(df)} candles available (minimum 20)")
                    continue

                df = df.sort_values('timestamp').reset_index(drop=True)

                evaluator = NodeEvaluator(bot.settings)
                evaluator.df = df.copy()
                evaluator._calculate_indicators()

                existing_timestamps = {_naive_utc(s[0]) for s in db.query(Signal.timestamp).filter(Signal.bot_name == bot.name, Signal.symbol == symbol).all()}

                open_bt_pos = None
                last_bt_ts = None

                if run_backtest:
                    blb.push(bot.name, "INFO", f"Running backtest on {len(df)} candles...")
                    self.set_runtime(bot.name, "backtesting", f"{symbol} · {len(df)} candles", symbol=symbol, symbol_index=sym_idx, symbol_count=len(symbols))
                else:
                    self.set_runtime(bot.name, "starting", f"Computing indicators for {symbol}…")
                    last_order = db.query(Order).filter(Order.bot_name == bot.name, Order.symbol == symbol, Order.mode == "backtest").order_by(Order.timestamp.desc()).first()
                    if last_order:
                        last_bt_ts = last_order.timestamp
                        if last_bt_ts.tzinfo is None: last_bt_ts = last_bt_ts.replace(tzinfo=timezone.utc)

                    open_bt_pos = db.query(Position).filter(Position.bot_name == bot.name, Position.symbol == symbol, Position.mode == "backtest", Position.status == "open").first()

                entry_series = evaluator.resolve_node(bot.settings.get("entry_node")) if bot.settings.get("entry_node") else pd.Series(False, index=evaluator.df.index)
                exit_series = evaluator.resolve_node(exit_node) if exit_node else pd.Series(False, index=evaluator.df.index)

                # Pre-extract numpy arrays once — avoids O(n) .iloc index lookups inside the loop
                _standard_cols = {'id', 'timestamp', 'open', 'high', 'low', 'close', 'volume', 'atr'}
                sym_contexts.append({
                    "symbol": symbol,
                    "df": evaluator.df,
                    "entry_arr": entry_series.values,
                    "exit_arr": exit_series.values,
                    "atr_arr": evaluator.df['atr'].values if 'atr' in evaluator.df.columns else None,
                    "indicator_cols": [c for c in evaluator.df.columns if c not in _standard_cols],
                    "existing_timestamps": existing_timestamps,
                    "last_bt_ts": last_bt_ts,
                    "open_pos": open_bt_pos,
                    "original_amount": None,  # for weighted profit_pct calculation
                    "trade_entry_indices": [],
                    "new_signals": [],
                    "last_close": None,
                })

            # Only stop when NO whitelist symbol produced usable data. If some
            # symbols have data, trade those and just warn about the empties
            # (a single bad/new pair shouldn't take the whole bot down).
            if not sym_contexts:
                reason = ", ".join(empty_symbols) if empty_symbols else "any configured symbol"
                logger.warning("Bot '%s' stopped: no candle data for %s", bot.name, reason)
                blb.push(bot.name, "ERROR", f"Stopped: no historical data for {reason}. Fix the pair/timeframe or try again once the exchange responds.")
                self._engine_stop(bot, db, f"No historical data for {reason}")
                db.commit()
                return
            if empty_symbols:
                blb.push(bot.name, "WARN", f"No data for {', '.join(empty_symbols)} — continuing with the remaining symbol(s).")

            if not self._still_active(bot_id, _run_token):
                raise self._StoppedByUser()

            # ── Merged chronological execution across all symbols ──
            timeline = []
            for ci, ctx in enumerate(sym_contexts):
                for idx, ts_val in enumerate(list(ctx["df"]['timestamp'])):
                    # Normalize sort keys — legacy rows can be tz-aware while new
                    # rows are naive, and mixed values are not comparable
                    timeline.append((_naive_utc(ts_val), ci, idx))
            timeline.sort(key=lambda t: (t[0], t[1]))

            max_pos = _int(bot.settings.get("max_positions"), 1)
            _tl_total = len(timeline)
            _tl_step = max(1, _tl_total // 40)
            if run_backtest:
                self.set_runtime(bot.name, "backtesting", f"Simulating {_tl_total} candles across {len(sym_contexts)} symbol(s)", {"done": 0, "total": _tl_total})

            for _tl_i, (_ts_key, ci, index) in enumerate(timeline):
                if run_backtest and _tl_i % _tl_step == 0:
                    self.set_runtime(bot.name, "backtesting", f"Simulating {_tl_i}/{_tl_total} candles", {"done": _tl_i, "total": _tl_total})
                    if not self._still_active(bot_id, _run_token):
                        raise self._StoppedByUser()
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

                    # Capital depletion halt / drawdown block: no new entries, exits keep running
                    if is_buy and not open_bt_pos and 1 <= max_pos and can_buy_cooldown and bt_equity > 0 and not bt_entries_blocked:
                        trade_amount = self._calculate_trade_amount(current_price, bot.settings, current_equity=bt_equity)
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
                        exit_events = self._check_exits(open_bt_pos, current_price, current_high, current_low, is_sell, bot.settings, current_atr, row_open=current_open)

                        for ev in exit_events:
                            open_bt_pos = ctx["open_pos"]
                            if open_bt_pos is None: break

                            if ev.get('close_amount_type') == 'fixed':
                                close_qty = min(ev['qty_pct'], open_bt_pos.amount)
                            else:
                                close_qty = open_bt_pos.amount * (ev['qty_pct'] / 100)
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

                            with self._position_states_lock:
                                if open_bt_pos.id in self.position_states:
                                    self.position_states[open_bt_pos.id]['triggered_exits'].add(ev['id'])
                                    open_bt_pos.triggered_exits = list(self.position_states[open_bt_pos.id]['triggered_exits'])

                            if close_qty >= open_bt_pos.amount - 0.00001:
                                open_bt_pos.status = "closed"
                                open_bt_pos.closed_at = _naive_utc(ts)
                                with self._position_states_lock:
                                    self.position_states.pop(open_bt_pos.id, None)
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

                    with self._position_states_lock:
                        self.position_states.pop(open_bt_pos.id, None)
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
            if not self._still_active(bot_id, _run_token):
                raise self._StoppedByUser()

            for ctx in sym_contexts:
                trade_count = len(ctx["trade_entry_indices"]) if run_backtest else 0
                logger.info("Backfill complete: '%s' on %s | mode=%s | %d candles | %d trades | equity=$%.2f", bot.name, ctx["symbol"], live_mode.upper(), len(ctx["df"]), trade_count, bt_equity)
                if run_backtest:
                    blb.push(bot.name, "INFO", f"Backtest complete: {ctx['symbol']} | {len(ctx['df'])} candles | {trade_count} trades | equity=${bt_equity:.2f}")
                else:
                    blb.push(bot.name, "INFO", f"Ready: {ctx['symbol']} | {len(ctx['df'])} candles | mode={live_mode.upper()}")

            # After the full chronological run, enforce max drawdown on the
            # mark-to-market equity curve before the bot is allowed to go live
            if run_backtest:
                # Persist the engine's measured drawdown so the analytics UI
                # can show the number the gate actually enforces (the
                # closed-trade curve in the UI understates intra-trade dips)
                try:
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
                        "return_pct": round(100.0 * sum(pnls) / bt_starting_capital, 2) if bt_starting_capital else 0.0,
                        "max_drawdown": round(bt_max_dd, 2),
                        "max_capital_loss": round(bt_max_loss, 2),
                        "entries_blocked_days": round(bt_blocked_secs / 86400, 1),
                        "entries_blocked_count": bt_block_count,
                        "candles": sum(len(c["df"]) for c in sym_contexts),
                        # Data range the backtest walked — lets the analytics page
                        # measure flat periods before the first / after the last trade
                        "data_from": timeline[0][0].isoformat() if timeline else None,
                        "data_to": timeline[-1][0].isoformat() if timeline else None,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                    bot.settings = {**bot.settings, "last_backtest_max_drawdown": round(bt_max_dd, 2), "last_backtest_summary": summary}
                    flag_modified(bot, "settings")
                    db.commit()
                except Exception:
                    db.rollback()

                max_drawdown_pct = _num(bot.settings.get("max_drawdown"), 0)
                max_capital_loss_pct = _num(bot.settings.get("max_capital_loss"), 0)
                self._drawdown_cache.pop((bot.name, "backtest"), None)

                # Loss of principal is the hard stop regardless of drawdown_action
                if max_capital_loss_pct > 0 and bt_max_loss >= max_capital_loss_pct:
                    logger.warning("Bot '%s' backtest capital loss (%.2f%%) exceeds max (%.2f%%), stopping before live", bot.name, bt_max_loss, max_capital_loss_pct)
                    blb.push(bot.name, "WARN", f"Backtest capital loss {bt_max_loss:.1f}% > {max_capital_loss_pct:.0f}% — bot stopped, not allowed to go live")
                    self._engine_stop(bot, db, f"Backtest capital loss {bt_max_loss:.1f}% exceeded the {max_capital_loss_pct:.0f}% limit")
                    db.commit()
                    return

                if max_drawdown_pct > 0 and bt_max_dd >= max_drawdown_pct:
                    _d = bt_dd_detail
                    _fmt = lambda t: t.strftime('%Y-%m-%d') if t is not None else '?'
                    detail = (f"peak {_fmt(_d['peak_ts'])} ${_d['peak_eq']:,.0f} -> trough {_fmt(_d['trough_ts'])} "
                              f"${_d['trough_eq']:,.0f}, {_d['open_at_trough']} open position(s)")
                    if bt_dd_action == "block_entries":
                        # Informative only: the same rule already paused entries
                        # inside the simulation, so the numbers reflect it
                        logger.warning("Bot '%s' backtest drawdown %.2f%% >= %.2f%% (block_entries) — going live with entries paused on breach", bot.name, bt_max_dd, max_drawdown_pct)
                        blb.push(bot.name, "WARN", f"Backtest max drawdown {bt_max_dd:.1f}% ({detail}). Entries were blocked {bt_block_count}x for {bt_blocked_secs / 86400:.0f} days in total (cooldown {bt_dd_cooldown_secs / 86400:.0f}d) — bot continues, new entries pause on breach.")
                    else:
                        logger.warning("Bot '%s' backtest drawdown (%.2f%%) exceeds max (%.2f%%), stopping before live", bot.name, bt_max_dd, max_drawdown_pct)
                        blb.push(bot.name, "WARN", f"Backtest max drawdown {bt_max_dd:.1f}% >= {max_drawdown_pct:.0f}% ({detail}), bot stopped — not allowed to go live")
                        self._engine_stop(bot, db, f"Backtest drawdown {bt_max_dd:.1f}% exceeded the {max_drawdown_pct:.0f}% limit")
                        db.commit()
                        return

            # Wallet report for real modes: balances of every whitelist token,
            # this bot's allocation and whether the key is over-allocated across
            # bots. The first successful report also freezes live_starting_capital
            # as the base for live drawdown / capital-loss percentages.
            if live_mode in ("paper", "live"):
                try:
                    _ccxt = self._get_ccxt_instance(api_key)
                    _bal = _ccxt.fetch_balance()
                    _pairs = [str(s).replace('-', '/').upper() for s in symbols]
                    _tokens = []
                    for _p in _pairs:
                        for _t in _p.split('/'):
                            if _t not in _tokens:
                                _tokens.append(_t)
                    def _free(t):
                        v = _bal.get(t)
                        return float((v or {}).get("free") or 0) if isinstance(v, dict) else float((_bal.get("free") or {}).get(t) or 0)
                    _parts = [f"{_free(t):,.4f}".rstrip('0').rstrip('.') + f" {t}" for t in _tokens]
                    _quote = _pairs[0].split('/')[-1] if _pairs else "USDT"
                    _pool, _wallet_total, _bot_total = self._live_allocation(db, bot, _quote, _free(_quote))
                    _pct = _num(bot.settings.get("live_allocation_pct"), 100)
                    blb.push(bot.name, "INFO", f"Wallet '{api_key.name}' ({live_mode}): {', '.join(_parts)} free — this bot: {_pct:.0f}% = {_bot_total:,.2f} {_quote} ({_pool:,.2f} still deployable)")
                    _peer_pct = sum(_num((b.settings or {}).get("live_allocation_pct"), 100)
                                    for b in db.query(BotConfig).filter(BotConfig.is_active == True).all()
                                    if (b.settings or {}).get("api_key_name") == api_key.name and (b.settings or {}).get("api_execution"))
                    if _peer_pct > 100.0:
                        blb.push(bot.name, "WARN", f"Bots on key '{api_key.name}' allocate {_peer_pct:.0f}% of the wallet in total — entries will compete for the same funds")
                    if not bot.settings.get("live_starting_capital") and _bot_total > 0:
                        bot.settings = {**bot.settings, "live_starting_capital": round(_bot_total, 2)}
                        db.commit()
                        blb.push(bot.name, "INFO", f"Live starting capital set to {_bot_total:,.2f} {_quote} (base for drawdown / capital-loss %; cleared by a cache wipe)")
                except Exception as _exc:
                    logger.warning("Bot '%s': wallet report failed: %s", bot.name, _exc)
                    blb.push(bot.name, "WARN", f"Could not read wallet balance: {_exc}")

            # Make the backtest→live handover visible in the console: the next
            # tick only arrives when the current candle closes on the exchange
            try:
                tf_secs = _tf_seconds(timeframe)
                next_close = datetime.fromtimestamp(((int(time.time()) // tf_secs) + 1) * tf_secs, tz=timezone.utc)
                blb.push(bot.name, "INFO", f"Live monitoring active ({live_mode}) — next {timeframe} candle closes ~{next_close.strftime('%H:%M')} UTC")
                self.set_runtime(bot.name, "live", f"Waiting for next {timeframe} candle close", mode=live_mode, next_close=next_close.isoformat())
            except Exception:
                blb.push(bot.name, "INFO", f"Live monitoring active ({live_mode}) — waiting for the next {timeframe} candle close")
                self.set_runtime(bot.name, "live", f"Waiting for next {timeframe} candle close", mode=live_mode)

        except self._StoppedByUser:
            db.rollback()
            if self._run_tokens.get(bot_id) is _run_token:
                blb.push(_log_name, "INFO", "Stopped by user — startup aborted.")
                self.clear_runtime(_log_name)
        except Exception as e:
            logger.error("Backfill Error: %s", e, exc_info=True)
            blb.push(_log_name, "ERROR", f"Backfill error: {e}")
            db.rollback()
            try:
                bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
                if bot and bot.is_active:
                    self._engine_stop(bot, db, f"Startup error: {str(e)[:160]}")
                    db.commit()
            except Exception:
                db.rollback()
        finally:
            # A superseded thread (restart) must not unmask the bot while the
            # replacement thread is still backfilling
            if self._run_tokens.get(bot_id) is _run_token:
                self._backfilling_bots.discard(_log_name)
            db.close()

    async def _process_bots(self, exchange: str, symbol: str, timeframe: str):
        def run_logic():
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
                ).order_by(Candle.timestamp.desc()).limit(max_lookback).statement

                df = pd.read_sql(query, db.bind)

                if df.empty or len(df) < 20:
                    return

                df = df.iloc[::-1].reset_index(drop=True)

                indicator_cache = {}  # fingerprint -> DataFrame with indicators computed

                # ── Batch pre-load: positions, orders counts (1 query each instead of N) ──
                matching_bot_names = [b.name for b in matching_bots if b.name not in self._deleted_bots and b.name not in self._backfilling_bots]

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

                for bot in matching_bots:
                    if bot.name in self._deleted_bots or bot.name in self._backfilling_bots:
                        continue

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
                        dd_state = self._get_drawdown(bot.name, db, mode_group="live", starting_capital=live_capital, peak_reset_at=_peak_reset_at)
                        dd_now, loss_now = self._dd_now(dd_state)

                        stop_reason = None
                        _open_any = sum(len(v) for k, v in _positions_by_bot_mode.items() if k[0] == bot.name and k[1] != "backtest")
                        if max_capital_loss_pct > 0 and loss_now >= max_capital_loss_pct:
                            if dd_action == "block_entries" and _open_any > 0:
                                # Wind down: no new entries, exits keep running,
                                # the bot stops on the tick it turns flat. Loss of
                                # principal never recovers without trades, so
                                # unlike the drawdown block there is no resume.
                                if bot.name not in self._entries_blocked:
                                    self._entries_blocked.add(bot.name)
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
                            self._close_all_open_positions(bot, db, key_records)
                            self._engine_stop(bot, db, stop_reason)
                            self._drawdown_cache.pop((bot.name, "live"), None)
                            self._drawdown_cache.pop((bot.name, "backtest"), None)
                            self._entries_blocked.discard(bot.name)
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
                            if bot.name not in self._entries_blocked and dd_now >= max_drawdown_pct:
                                self._entries_blocked.add(bot.name)
                                logger.warning("Bot '%s' drawdown %.2f%% > %.2f%% — new entries blocked", bot.name, dd_now, max_drawdown_pct)
                                blb.push(bot.name, "WARN", f"Max drawdown {dd_now:.1f}% > {max_drawdown_pct:.0f}% — new entries blocked (open positions keep their exits; resumes below {max_drawdown_pct * 0.5:.1f}% or after {_cooldown_days:.0f}d flat)")
                            elif bot.name in self._entries_blocked:
                                if dd_now < max_drawdown_pct * 0.5:
                                    self._entries_blocked.discard(bot.name)
                                    blb.push(bot.name, "INFO", f"Drawdown recovered to {dd_now:.1f}% — new entries allowed again")
                                elif _open_any == 0:
                                    _last_close = db.query(func.max(Position.closed_at)).filter(
                                        Position.bot_name == bot.name, Position.status == "closed",
                                        Position.mode.in_(["forward_test", "paper", "live"])).scalar()
                                    _now_ts = _naive_utc(datetime.now(timezone.utc))
                                    _flat_secs = (_now_ts - _last_close).total_seconds() if _last_close is not None else float("inf")
                                    if _flat_secs >= _cooldown_days * 86400:
                                        self._entries_blocked.discard(bot.name)
                                        bot.settings = {**bot.settings, "drawdown_peak_reset_at": _now_ts.isoformat()}
                                        self._drawdown_cache.pop((bot.name, "live"), None)
                                        db.commit()
                                        blb.push(bot.name, "INFO", f"Flat for {_cooldown_days:.0f} days at {dd_now:.1f}% drawdown — peak reset, new entries allowed again (capital-loss guard remains)")
                    entries_blocked = bot.name in self._entries_blocked

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
                        _prev_rt = self.get_runtime(bot.name) or {}
                        self.set_runtime(bot.name, "live", f"Last tick {symbol} @ {current_price:g} — {tick_action}",
                                         mode=_prev_rt.get("mode"), next_close=_next.isoformat(), last_tick_at=datetime.now(timezone.utc).isoformat())
                    except Exception:
                        pass

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
                            _cached_ccxt = self._get_ccxt_instance(api_key_record)
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

                    if is_buy and entries_blocked:
                        blb.push(bot.name, "INFO", f"BUY signal on {symbol} skipped — entries blocked by max drawdown")
                    elif is_buy and open_count < max_pos and can_buy_cooldown:
                        trade_amount = self._calculate_trade_amount(current_price, bot.settings)
                        if trade_amount is None:
                            logger.warning("Skipping buy for %s: invalid trade amount", symbol)
                        else:
                            try:
                                actual_price = current_price
                                order_id = f"local_{int(latest_time.timestamp())}_{uuid.uuid4().hex[:8]}"

                                buy_fee = 0.0
                                if mode in ["paper", "live"] and api_key_record:
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
                                        continue

                                    # Size trades from the wallet: this bot's share
                                    # (live_allocation_pct) of the quote equity, minus
                                    # what it already has deployed — same pool logic
                                    # as the backtest's bt_equity
                                    free_balance = self._get_live_capital(ccxt_inst, api_key_record, ccxt_symbol, bot.name)
                                    if free_balance is None:
                                        if mode == "live":
                                            logger.warning("Skipping entry for %s: could not verify exchange balance", symbol)
                                            blb.push(bot.name, "WARN", "Skipping entry: could not verify exchange balance")
                                            continue
                                        sizing_capital = _num(bot.settings.get("backtest_capital"), 1000)
                                        logger.info("Bot '%s': sandbox balance unavailable, sizing paper entry from backtest capital $%.2f", bot.name, sizing_capital)
                                    else:
                                        _quote = ccxt_symbol.split('/')[-1]
                                        pool, wallet_total, bot_total = self._live_allocation(db, bot, _quote, free_balance)
                                        if pool <= 0:
                                            blb.push(bot.name, "WARN", f"BUY signal on {symbol} skipped — allocation fully deployed ({bot_total:,.0f} {_quote} = {_num(bot.settings.get('live_allocation_pct'), 100):.0f}% of wallet {wallet_total:,.0f})")
                                            continue
                                        sizing_capital = min(free_balance, pool)
                                        logger.info("Bot '%s': sizing %s entry from $%.2f (free=$%.2f, pool remaining=$%.2f of allocation $%.2f, wallet=$%.2f)",
                                            bot.name, mode, sizing_capital, free_balance, pool, bot_total, wallet_total)
                                    trade_amount = self._calculate_trade_amount(current_price, bot.settings, current_equity=sizing_capital)
                                    if trade_amount is None:
                                        logger.warning("Skipping buy for %s: no capital available to size trade", symbol)
                                        continue
                                    trade_amount = float(ccxt_inst.amount_to_precision(ccxt_symbol, trade_amount))
                                    if trade_amount <= 0:
                                        logger.warning("Trade amount rounded to zero for %s after precision, skipping", ccxt_symbol)
                                        continue
                                    min_violation = self._below_market_minimum(ccxt_inst, ccxt_symbol, trade_amount, current_price)
                                    if min_violation:
                                        logger.warning("%s BUY skipped for %s: %s", mode.upper(), symbol, min_violation)
                                        blb.push(bot.name, "WARN", f"{min_violation} — increase trade size")
                                        continue
                                    # Safety guard: reject orders exceeding max_order_value
                                    max_order_usd = _num(bot.settings.get("max_order_value"), 0)
                                    if max_order_usd > 0:
                                        order_value_usd = trade_amount * current_price
                                        if order_value_usd > max_order_usd:
                                            logger.warning("SAFETY: BUY order $%.2f exceeds max_order_value $%.2f for %s. Skipping.", order_value_usd, max_order_usd, symbol)
                                            continue
                                    okx_order = ccxt_inst.create_market_buy_order(ccxt_symbol, trade_amount)
                                    logger.info("%s BUY response: id=%s status=%s filled=%s avg=%s fee=%s",
                                        mode.upper(), okx_order.get("id"), okx_order.get("status"),
                                        okx_order.get("filled"), okx_order.get("average"), okx_order.get("fee"))
                                    okx_order = self._reconcile_order(ccxt_inst, okx_order, ccxt_symbol)
                                    filled_qty = float(okx_order.get("filled") or 0)
                                    if filled_qty <= 0 and okx_order.get("status") != "closed":
                                        if self._cancel_unfilled_order(ccxt_inst, okx_order.get("id"), ccxt_symbol):
                                            logger.warning("%s BUY unfilled (status=%s), canceled on exchange.", mode.upper(), okx_order.get("status"))
                                            db.add(Order(exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="buy", order_type="market", price=current_price, amount=trade_amount, timestamp=latest_time, exchange_order_id=okx_order.get("id"), status="canceled"))
                                            db.commit()
                                            continue
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
                                    buy_fee = self._fee_in_quote(okx_order.get("fee"), ccxt_symbol, actual_price)
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
                                    self._balance_cache.pop((api_key_record.name, ccxt_symbol.split('/')[-1]), None)

                                # Position created after successful exchange order
                                open_position = Position(exchange=exchange, bot_name=bot.name, symbol=symbol, mode=mode, status="open", side="long", entry_price=actual_price, amount=trade_amount)
                                db.add(open_position)
                                db.flush()
                                just_opened_ids.add(open_position.id)

                                db.add(Order(position_id=open_position.id, exchange=exchange, bot_name=bot.name, mode=mode, symbol=symbol, side="buy", order_type="market", price=actual_price, amount=trade_amount, timestamp=latest_time, exchange_order_id=order_id, status="filled", fee=buy_fee))
                                if mode in ["paper", "live"]:
                                    # A real exchange fill must be persisted immediately —
                                    # a later rollback may not erase the record of it
                                    db.commit()
                                logger.info("%s BUY Filled @ %s", mode.upper(), actual_price)
                                blb.push(bot.name, "INFO", f"{mode.upper()} BUY {symbol} @ {actual_price}")

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

                    # Use pre-loaded positions (already includes orders via selectinload)
                    active_positions = bot_positions

                    for pos in active_positions:
                        if not bot.is_active: break
                        if pos.id in just_opened_ids: continue

                        exit_events = self._check_exits(pos, current_price, current_high, current_low, is_sell, bot.settings, current_atr, row_open=current_open)

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
                                close_qty = pos.amount * (ev['qty_pct'] / 100)
                            close_qty = min(close_qty, pos.amount)
                            if close_qty <= 0: continue

                            try:
                                actual_price = ev['price']
                                order_id = f"local_{int(latest_time.timestamp())}_{uuid.uuid4().hex[:8]}"

                                actual_fee = 0.0
                                if mode in ["paper", "live"] and api_key_record:
                                    ccxt_inst = get_ccxt()
                                    close_qty = float(ccxt_inst.amount_to_precision(ccxt_symbol, close_qty))
                                    if close_qty <= 0:
                                        logger.warning("Sell amount rounded to zero for %s after precision, skipping", ccxt_symbol)
                                        continue
                                    min_violation = self._below_market_minimum(ccxt_inst, ccxt_symbol, close_qty, ev['price'])
                                    if min_violation:
                                        if self._below_market_minimum(ccxt_inst, ccxt_symbol, pos.amount, ev['price']):
                                            # The whole remainder can never be sold on the
                                            # exchange; close the position administratively
                                            # instead of retrying a doomed sell forever
                                            logger.warning("%s position remainder on %s unsellable (%s) — closing administratively", mode.upper(), symbol, min_violation)
                                            blb.push(bot.name, "WARN", f"Position remainder on {symbol} below exchange minimum ({min_violation}) — closed administratively, dust remains on the exchange")
                                            pos.status = "closed"
                                            pos.closed_at = latest_time
                                            self._update_drawdown(bot.name, "live", pos.profit_abs)
                                            with self._position_states_lock:
                                                self.position_states.pop(pos.id, None)
                                            db.commit()
                                            break
                                        logger.warning("%s SELL skipped for %s: %s", mode.upper(), symbol, min_violation)
                                        blb.push(bot.name, "WARN", f"Sell on {symbol} skipped: {min_violation}")
                                        continue
                                    okx_order = ccxt_inst.create_market_sell_order(ccxt_symbol, close_qty)
                                    logger.info("%s SELL response: id=%s status=%s filled=%s avg=%s fee=%s",
                                        mode.upper(), okx_order.get("id"), okx_order.get("status"),
                                        okx_order.get("filled"), okx_order.get("average"), okx_order.get("fee"))
                                    okx_order = self._reconcile_order(ccxt_inst, okx_order, ccxt_symbol)
                                    filled_qty = float(okx_order.get("filled") or 0)
                                    if filled_qty <= 0 and okx_order.get("status") != "closed":
                                        if self._cancel_unfilled_order(ccxt_inst, okx_order.get("id"), ccxt_symbol):
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
                                        self._engine_stop(bot, db, f"Sell order state unknown on {symbol} — verify on the exchange before restarting")
                                        db.commit()
                                        break
                                    # Book only what actually sold so a partial fill
                                    # reduces the position pro rata instead of being
                                    # retried for the full amount later
                                    if filled_qty > 0:
                                        close_qty = min(filled_qty, close_qty)
                                    actual_price = okx_order.get("average") or okx_order.get("price") or ev['price']
                                    order_id = okx_order.get("id")
                                    actual_fee = self._fee_in_quote(okx_order.get("fee"), ccxt_symbol, actual_price)
                                    self._balance_cache.pop((api_key_record.name, ccxt_symbol.split('/')[-1]), None)

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

                                with self._position_states_lock:
                                    if pos.id in self.position_states:
                                        self.position_states[pos.id]['triggered_exits'].add(ev['id'])
                                        pos.triggered_exits = list(self.position_states[pos.id]['triggered_exits'])

                                if close_qty >= pos.amount - 0.00001:
                                    pos.status = "closed"
                                    pos.closed_at = latest_time
                                    self._update_drawdown(bot.name, "live", pos.profit_abs)
                                    with self._position_states_lock:
                                        self.position_states.pop(pos.id, None)
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

        await asyncio.to_thread(run_logic)

    def mark_deleted(self, bot_name: str):
        """Mark a bot as deleted so _process_bots skips it."""
        self._deleted_bots.add(bot_name)
        self._drawdown_cache.pop((bot_name, "live"), None)
        self._drawdown_cache.pop((bot_name, "backtest"), None)
        # Purge position states for this bot to prevent memory accumulation
        try:
            db = SessionLocal()
            pos_ids = {p.id for p in db.query(Position.id).filter(Position.bot_name == bot_name).all()}
            db.close()
            with self._position_states_lock:
                for pid in pos_ids:
                    self.position_states.pop(pid, None)
        except Exception:
            pass

    def reset_bot_state(self, bot_name: str):
        """Drop every in-memory cache the engine keeps for a bot (cache wipe):
        drawdown state, entry block, position states of positions that no
        longer exist. Open real positions keep their state."""
        self._drawdown_cache.pop((bot_name, "live"), None)
        self._drawdown_cache.pop((bot_name, "backtest"), None)
        self._entries_blocked.discard(bot_name)
        self._balance_cache.clear()
        try:
            db = SessionLocal()
            existing = {p.id for p in db.query(Position.id).all()}
            db.close()
            with self._position_states_lock:
                for pid in [k for k in self.position_states if k not in existing]:
                    self.position_states.pop(pid, None)  # position row is gone → stale state
        except Exception as e:
            logger.warning(f"reset_bot_state({bot_name}): could not prune position states: {e}")

    def unmark_deleted(self, bot_name: str):
        """Remove deletion marker after cleanup is complete."""
        self._deleted_bots.discard(bot_name)

    def stop(self):
        self.running = False
        logger.info("Bot Manager stopped.")

bot_manager = BotManager()
