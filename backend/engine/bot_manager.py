"""BotManager: the engine's state holder and async front door. The work is
in the sibling modules — sizing (settings/fingerprints/trade size), exits
(SL/TP rules), risk (drawdown tracking), broker (ccxt plumbing), backtest
(chronological simulation + gate), startup (the per-bot start thread) and
live_cycle (one live candle for every bot). The manager keeps the locks,
caches and runtime status they share, and the thin `_x` methods below so
routers and tests keep one stable seam."""
import asyncio
import logging
import threading
from datetime import datetime, timezone
from collections import defaultdict
from sqlalchemy.orm.attributes import flag_modified
from backend.core.database import SessionLocal
from backend.models.bots import BotConfig
from backend.models.positions import Position
from backend.models.exchange_keys import ExchangeKey
from backend.core.events import event_bus
from backend.engine import sizing, exits, risk, broker, startup, live_cycle
from backend.engine.exits import VALID_EXIT_TYPES  # noqa: F401 — re-exported
from backend.engine.sizing import (  # noqa: F401 — re-exported for routers/tests
    _indicator_fingerprint, _num, _int, _tf_seconds, _naive_utc,
    _config_fingerprint, _record_config_run,
)

logger = logging.getLogger("apexalgo.bot_manager")


class BotManager:
    def __init__(self):
        self.running = False
        self.position_states = {}
        # position_states is mutated from backfill threads and _process_bots
        # worker threads, so a threading lock (not asyncio) guards it
        self._position_states_lock = threading.Lock()
        self._risk = risk.DrawdownTracker()
        self._drawdown_cache = self._risk.cache  # (bot_name, mode_group) -> drawdown state
        self._drawdown_lock = self._risk.lock
        # Bots whose max_drawdown breached with drawdown_action=block_entries:
        # no new entries until drawdown recovers below half the limit. Purely
        # in-memory — re-derived from the drawdown cache on the first tick
        # after a restart, so it survives without persistence.
        self._entries_blocked = set()
        self._deleted_bots = set()  # bot names pending cleanup, skip in processing
        self._backfilling_bots = set()  # bot names currently in backfill, skip in live processing
        self._candle_locks = defaultdict(asyncio.Lock)  # (exchange, symbol, timeframe) -> serializer
        self._bot_locks = defaultdict(threading.Lock)  # bot_name -> serializer across symbol ticks
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
        except Exception as e:
            logger.warning("Could not persist last_stop_reason for %s: %s", bot.name, e)
        self.set_runtime(bot.name, "halted", reason)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # Drawdown state lives in engine/risk.py; the dict and lock are shared by
    # reference so routers keep invalidating `_drawdown_cache` directly
    def _get_drawdown(self, bot_name, db, mode_group="live", starting_capital=1000.0, peak_reset_at=None):
        return self._risk.get(bot_name, db, mode_group, starting_capital, peak_reset_at)

    def _update_drawdown(self, bot_name, mode_group, profit_abs):
        self._risk.update(bot_name, mode_group, profit_abs)

    def _dd_now(self, state, unrealized=0.0):
        return self._risk.now(state, unrealized)

    _unrealized_pnl = staticmethod(risk.unrealized_pnl)

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
            await self._process_bots(exchange, symbol, timeframe, candle_ts)
            if candle_ts is not None:
                self._processed_candles[key] = candle_ts

    # ── Exchange plumbing lives in engine/broker.py ──
    # `_get_ccxt_instance` and `_reconcile_order` stay real methods: tests
    # monkeypatch them per instance to inject a fake exchange
    def _get_ccxt_instance(self, api_key_record: ExchangeKey, symbol=None):
        return broker.get_ccxt_instance(api_key_record, symbol)

    @staticmethod
    def _needs_own_instance(api_key_record: ExchangeKey, symbol) -> bool:
        """True when `symbol` is served by a different ccxt class than the
        key's default one (binance inverse -> binancecoinm)."""
        from backend.core.exchange_registry import ccxt_id_for, key_kind_for_symbol, key_market_type
        if not symbol or key_market_type(api_key_record) != "swap":
            return False
        kind = key_kind_for_symbol(api_key_record, symbol)
        ex = api_key_record.exchange
        return ccxt_id_for(ex, "swap", kind) != ccxt_id_for(ex, "swap")

    def _ccxt_for(self, api_key_record: ExchangeKey, symbol=None):
        """Client for `symbol`: the key's shared instance unless the contract
        kind lives on its own ccxt class. Tests that monkeypatch
        `_get_ccxt_instance` with a one-argument fake keep working because
        the symbol is only passed when it actually matters."""
        if self._needs_own_instance(api_key_record, symbol):
            return self._get_ccxt_instance(api_key_record, symbol)
        return self._get_ccxt_instance(api_key_record)

    def _reconcile_order(self, ccxt_inst, order, ccxt_symbol, attempts=5, delay=1.0):
        return broker.reconcile_order(ccxt_inst, order, ccxt_symbol, attempts, delay)

    def _cancel_unfilled_order(self, ccxt_inst, order_id, ccxt_symbol):
        return broker.cancel_unfilled_order(ccxt_inst, order_id, ccxt_symbol)

    _below_market_minimum = staticmethod(broker.below_market_minimum)
    _fee_in_quote = staticmethod(broker.fee_in_quote)
    _fee_cash = staticmethod(broker.fee_cash)

    # Sizing helpers live in engine/sizing.py; kept as attributes so callers
    # and tests keep addressing them through the manager
    _sim_frictions = staticmethod(sizing.sim_frictions)
    _deployed_capital = staticmethod(sizing.deployed_capital)
    _forward_pool = staticmethod(sizing.forward_pool)
    _live_allocation = staticmethod(sizing.live_allocation)
    _calculate_trade_amount = staticmethod(sizing.calculate_trade_amount)

    _wallet_held = staticmethod(broker.wallet_held)
    _reconcile_positions_with_wallet = staticmethod(broker.reconcile_positions_with_wallet)
    # Derivatives (phase 2): position reconciliation via fetch_positions,
    # leverage/margin-mode confirmation and contract conversion
    _reconcile_positions_with_exchange = staticmethod(broker.reconcile_positions_with_exchange)
    _ensure_leverage = staticmethod(broker.ensure_leverage)
    _to_contracts = staticmethod(broker.to_contracts)
    _from_contracts = staticmethod(broker.from_contracts)

    def _get_live_capital(self, ccxt_inst, api_key_record, ccxt_symbol, bot_name, ttl=30):
        return broker.get_live_capital(self._balance_cache, ccxt_inst, api_key_record, ccxt_symbol, bot_name, ttl)

    def _close_all_open_positions(self, bot, db, key_records):
        live_cycle.close_all_open_positions(self, bot, db, key_records)

    def _check_exits(self, open_position, row_close, row_high, row_low, is_sell_signal, bot_settings, current_atr=0.0, row_open=None, side="long"):
        return exits.check_exits(self.position_states, self._position_states_lock, open_position,
                                 row_close, row_high, row_low, is_sell_signal, bot_settings, current_atr, row_open, side=side)

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

    # ── Startup thread lives in engine/startup.py ──
    def _execute_sync_backfill(self, bot_id: int):
        startup.execute_sync_backfill(self, bot_id)

    async def _process_bots(self, exchange: str, symbol: str, timeframe: str, candle_ts=None):
        """One live candle for every matching bot — see engine/live_cycle.py."""
        await asyncio.to_thread(live_cycle.process_tick, self, exchange, symbol, timeframe, candle_ts)

    def mark_deleted(self, bot_name: str):
        """Mark a bot as deleted so _process_bots skips it."""
        self._deleted_bots.add(bot_name)
        for _grp in risk.MODES_BY_GROUP:
            self._drawdown_cache.pop((bot_name, _grp), None)
        # Purge position states for this bot to prevent memory accumulation
        try:
            db = SessionLocal()
            pos_ids = {p.id for p in db.query(Position.id).filter(Position.bot_name == bot_name).all()}
            db.close()
            with self._position_states_lock:
                for pid in pos_ids:
                    self.position_states.pop(pid, None)
        except Exception as e:
            logger.warning("mark_deleted(%s): could not purge position states: %s", bot_name, e)

    def reset_bot_state(self, bot_name: str):
        """Drop every in-memory cache the engine keeps for a bot (cache wipe):
        drawdown state, entry block, position states of positions that no
        longer exist. Open real positions keep their state."""
        for _grp in risk.MODES_BY_GROUP:
            self._drawdown_cache.pop((bot_name, _grp), None)
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
