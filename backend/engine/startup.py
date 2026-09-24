"""Bot startup: the thread that runs between "Start" and the first live
tick — data wait, indicator warm-up, backtest + gate, wallet report and
reconciliation. Runs under `asyncio.to_thread`; a stop/restart mid-way
invalidates the run token and aborts it with StoppedByUser."""
import logging
import time
from datetime import datetime, timezone
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified
from backend.core.database import SessionLocal
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.signals import Signal
from backend.models.orders import Order
from backend.models.positions import Position
from backend.models.exchange_keys import ExchangeKey
from backend.engine.evaluator import NodeEvaluator
from backend.engine import backtest
from backend.engine.sizing import _num, _int, _tf_seconds, _naive_utc, backtest_pin, data_fingerprint
from backend.engine.symbols import DEFAULT_MARGIN_MODE, base_of, cash_currency, is_derivative, leverage_for, market_type_for
from backend.core.exchange_registry import get_exchange_timeframes
from backend.core import bot_log_buffer as blb

logger = logging.getLogger("apexalgo.bot_manager")


def flush_backtest_data(engine, db, bot_name: str):
    """Remove a bot's previous backtest results (signals + backtest-mode
    positions/orders) so a new run simulates the full window cleanly.
    Live/paper/forward positions are untouched. Chunked deletes keep the
    write-lock short next to concurrent backfill commits."""
    try:
        for table, where in (
            ("signals", "bot_name = :bn"),
            ("orders", "bot_name = :bn AND mode = 'backtest'"),
            ("positions", "bot_name = :bn AND mode = 'backtest'"),
        ):
            while True:
                res = db.execute(text(
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE {where} LIMIT 20000)"
                ), {"bn": bot_name})
                db.commit()
                if res.rowcount == 0:
                    break
        engine._drawdown_cache.pop((bot_name, "backtest"), None)
    except Exception as exc:
        db.rollback()
        logger.warning("Could not flush previous backtest data for '%s': %s", bot_name, exc)

def still_active(engine, bot_id: int, token=None) -> bool:
    """Fresh-session check so a user stop during backfill/backtest is
    honoured. A stop+start (restart) issues a new run token, so the
    superseded thread also bails out instead of running twice."""
    if token is not None and engine._run_tokens.get(bot_id) != token:
        return False
    _db = SessionLocal()
    try:
        row = _db.query(BotConfig.is_active).filter(BotConfig.id == bot_id).first()
        return bool(row and row[0])
    finally:
        _db.close()

class StoppedByUser(Exception):
    """Raised inside the startup thread when the bot was stopped or restarted."""

def execute_sync_backfill(engine, bot_id: int):
    """Startup thread of one bot: wait for candle data per whitelist symbol,
    compute indicators, run the backtest (engine/backtest.py) and its gate,
    report/reconcile the wallet for real modes, then hand over to live ticks.
    `engine` is the BotManager whose runtime/lock state this thread drives."""
    db = SessionLocal()
    _log_name = f"bot_id={bot_id}"
    _run_token = None
    run_backtest = False
    try:
        bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
        if not bot or not bot.is_active: return
        _log_name = bot.name
        _run_token = object()
        engine._run_tokens[bot_id] = _run_token
        engine._backfilling_bots.add(bot.name)

        def _check_abort():
            if not still_active(engine, bot_id, _run_token):
                raise StoppedByUser()
        engine.set_runtime(bot.name, "starting", "Preparing engine…")

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
        # A pinned window replays exactly the candles of a saved run instead
        # of the newest `lookback` ones, so the result is reproducible until
        # the user explicitly reruns against the latest data
        pin_from, pin_to = backtest_pin(bot.settings) if run_backtest else (None, None)
        pinned = pin_from is not None

        if run_backtest:
            # A backtest is deterministic, so always simulate the whole
            # window from scratch. Stitching a new run onto leftovers of a
            # previous one (different data range or exchange) produces a
            # patchwork of trades with a double capital start.
            flush_backtest_data(engine, db, bot.name)

        symbols = bot.settings.get("symbols", [])
        if not symbols and bot.settings.get("symbol"):
            symbols = [bot.settings.get("symbol")]

        # Per-symbol data prep first (indicators stay per symbol); execution
        # then runs over one merged timeline so all symbols contend for the
        # shared capital pool in chronological order.
        sym_contexts = []
        empty_symbols = []

        for symbol in symbols:
            _window = f"pinned {pin_from:%Y-%m-%d} → {pin_to:%Y-%m-%d}" if pinned else f"lookback={lookback_limit}"
            blb.push(bot.name, "INFO", f"Starting: {symbol} | {timeframe} | {live_mode} | {_window}")
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
                engine.set_runtime(bot.name, "fetching", f"{symbol} · {initial_count}/{lookback_limit} candles",
                                 {"done": initial_count, "total": lookback_limit}, symbol=symbol, symbol_index=sym_idx, symbol_count=len(symbols))

                max_wait = 300  # 5 minutes max
                waited = 0
                stable_checks = 0
                last_count = initial_count
                last_log_count = initial_count

                while waited < max_wait:
                    time.sleep(2)
                    waited += 2
                    if not still_active(engine, bot_id, _run_token):
                        raise StoppedByUser()
                    current_count = _count_candles()
                    if current_count != last_count:
                        engine.set_runtime(bot.name, "fetching", f"{symbol} · {current_count}/{lookback_limit} candles",
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
                )
                if pinned:
                    query = query.filter(Candle.timestamp >= pin_from, Candle.timestamp <= pin_to).order_by(Candle.timestamp.asc())
                else:
                    query = query.order_by(Candle.timestamp.desc()).limit(lookback_limit)
                df = pd.read_sql(query.statement, candle_db.bind)
            finally:
                candle_db.close()

            if df.empty or len(df) < 20:
                logger.info("Skipping backfill for %s: insufficient data (%d candles, minimum 20 required).", symbol, len(df))
                _hint = " in the pinned window — rerun against latest or re-download the range" if pinned else ""
                blb.push(bot.name, "WARN", f"Skipping backtest: {symbol} — only {len(df)} candles available (minimum 20){_hint}")
                continue

            df = df.sort_values('timestamp').reset_index(drop=True)
            # Hash of the raw candles before any indicator touches them: the
            # summary compares it with the previous run on the same slice
            data_hash = data_fingerprint(df) if run_backtest else None

            evaluator = NodeEvaluator(bot.settings)
            evaluator.df = df.copy()
            evaluator._calculate_indicators()

            existing_timestamps = {_naive_utc(s[0]) for s in db.query(Signal.timestamp).filter(Signal.bot_name == bot.name, Signal.symbol == symbol).all()}

            open_bt_positions = []
            last_bt_ts = None

            if run_backtest:
                blb.push(bot.name, "INFO", f"Running backtest on {len(df)} candles...")
                engine.set_runtime(bot.name, "backtesting", f"{symbol} · {len(df)} candles", symbol=symbol, symbol_index=sym_idx, symbol_count=len(symbols))
            else:
                engine.set_runtime(bot.name, "starting", f"Computing indicators for {symbol}…")
                last_order = db.query(Order).filter(Order.bot_name == bot.name, Order.symbol == symbol, Order.mode == "backtest").order_by(Order.timestamp.desc()).first()
                if last_order:
                    last_bt_ts = last_order.timestamp
                    if last_bt_ts.tzinfo is None: last_bt_ts = last_bt_ts.replace(tzinfo=timezone.utc)

                open_bt_positions = db.query(Position).filter(Position.bot_name == bot.name, Position.symbol == symbol, Position.mode == "backtest", Position.status == "open").order_by(Position.id).all()

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
                "data_hash": data_hash,
                "last_bt_ts": last_bt_ts,
                "open_positions": open_bt_positions,  # pyramided up to max_positions
                "original_amount": {},  # pos.id -> entry size, for weighted profit_pct / partial exits
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
            engine._engine_stop(bot, db, f"No historical data for {reason}")
            db.commit()
            return
        if empty_symbols:
            blb.push(bot.name, "WARN", f"No data for {', '.join(empty_symbols)} — continuing with the remaining symbol(s).")

        if not still_active(engine, bot_id, _run_token):
            raise StoppedByUser()

        res = backtest.simulate(
            db, bot, sym_contexts, exchange_name, run_backtest,
            check_exits=engine._check_exits, position_states=engine.position_states, states_lock=engine._position_states_lock,
            on_progress=lambda detail, progress=None: engine.set_runtime(bot.name, "backtesting", detail, progress),
            check_abort=_check_abort)
        bt_equity = res.equity

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
            # can show the number the gate actually enforces
            try:
                summary = backtest.build_summary(db, bot, res, sym_contexts, exchange_name)
                bot.settings = {**bot.settings, "last_backtest_max_drawdown": round(res.max_dd, 2), "last_backtest_summary": summary}
                flag_modified(bot, "settings")
                db.commit()
                _sl = summary.get("variants_on_slice") or 0
                _tot = summary.get("variants") or 0
                blb.push(bot.name, "INFO", f"Backtest variant #{_sl} on this slice ({_tot} distinct config{'s' if _tot != 1 else ''} for this bot in total)")
                if summary.get("data_changed"):
                    blb.push(bot.name, "WARN", "Historical data changed since the previous run on this slice — the candles "
                                               "underneath differ (re-download, gap repair or exchange restatement), so the "
                                               "results are not directly comparable")
            except Exception as _exc:
                db.rollback()
                logger.warning("Bot '%s': could not persist backtest summary: %s", bot.name, _exc)

            engine._drawdown_cache.pop((bot.name, "backtest"), None)
            stop_reason = backtest.gate_stop_reason(bot, res)
            if stop_reason:
                engine._engine_stop(bot, db, stop_reason)
                db.commit()
                return

        # Wallet report for real modes: balances of every whitelist token,
        # this bot's allocation and whether the key is over-allocated across
        # bots. The first successful report also freezes live_starting_capital
        # as the base for live drawdown / capital-loss percentages.
        if live_mode in ("paper", "live"):
            try:
                _ccxt = engine._get_ccxt_instance(api_key)
                _bal = _ccxt.fetch_balance()
                _pairs = [str(s).replace('-', '/').upper() for s in symbols]
                # The backtest ran on public (production) candles; a demo
                # account or another region can list fewer pairs, and an
                # order on a missing one can only fail
                _missing = [p for p in _pairs if _ccxt.markets and p not in _ccxt.markets]
                if _missing:
                    blb.push(bot.name, "WARN", f"Not listed on {api_key.exchange.upper()}{' demo' if api_key.is_sandbox else ''} for key '{api_key.name}': {', '.join(_missing)} — {live_mode} entries on these pairs will be skipped")
                _tokens = []
                for _p in _pairs:
                    # Derivatives hold no base coin: only the settle currency matters
                    for _t in ([cash_currency(_p)] if is_derivative(_p) else [base_of(_p), cash_currency(_p)]):
                        if _t not in _tokens:
                            _tokens.append(_t)
                def _free(t):
                    v = _bal.get(t)
                    return float((v or {}).get("free") or 0) if isinstance(v, dict) else float((_bal.get("free") or {}).get(t) or 0)
                _parts = [f"{_free(t):,.4f}".rstrip('0').rstrip('.') + f" {t}" for t in _tokens]
                _quote = cash_currency(_pairs[0]) if _pairs else "USDT"
                _pool, _wallet_total, _bot_total = engine._live_allocation(db, bot, _quote, _free(_quote))
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
                _bal = None

            # Startup reconciliation: open DB positions must still be backed
            # by the exchange balance, otherwise exits would be sent for
            # coins that are no longer there
            if _bal is not None:
                try:
                    if market_type_for(bot.settings, api_key) != "spot":
                        # A perp position is not a coin in the wallet: compare
                        # against the exchange's open positions instead
                        _problems = engine._reconcile_positions_with_exchange(db, bot, _ccxt, live_mode)
                    else:
                        _problems = engine._reconcile_positions_with_wallet(db, bot, _ccxt, _bal, live_mode)
                except Exception as _exc:
                    logger.warning("Bot '%s': position reconciliation failed: %s", bot.name, _exc)
                    _problems = []
                if _problems:
                    for _p in _problems:
                        blb.push(bot.name, "ERROR", f"{_p} — reconcile manually (close or delete the position in Analytics) before starting")
                    engine._engine_stop(bot, db, f"{_problems[0]} — reconcile manually")
                    db.commit()
                    return

            # Derivatives: confirm leverage and margin mode on the exchange
            # for every pair before the first order — a bot that cannot set
            # what it backtested with must not trade
            _mt = market_type_for(bot.settings, api_key)
            if _mt != "spot":
                _lev = leverage_for(bot.settings, _mt)
                _mm = bot.settings.get("margin_mode") or DEFAULT_MARGIN_MODE
                try:
                    _ccxt = engine._get_ccxt_instance(api_key)
                    for _p in [str(s).replace('-', '/').upper() for s in symbols]:
                        if _ccxt.markets and _p not in _ccxt.markets:
                            continue
                        engine._ensure_leverage(_ccxt, api_key, _p, _lev, _mm, bot.name)
                except Exception as _exc:
                    logger.error("Bot '%s': could not apply leverage: %s", bot.name, _exc)
                    blb.push(bot.name, "ERROR", f"Could not set {_lev:g}x {_mm} on {api_key.exchange.upper()}: {_exc}")
                    engine._engine_stop(bot, db, f"Could not set {_lev:g}x {_mm} leverage on the exchange: {_exc}")
                    db.commit()
                    return
                blb.push(bot.name, "INFO", f"Perpetual swap ({live_mode}): {_lev:g}x {_mm} confirmed on {api_key.exchange.upper()} for {len(symbols)} pair(s) — funding payments are not modelled")

        # Make the backtest→live handover visible in the console: the next
        # tick only arrives when the current candle closes on the exchange
        try:
            tf_secs = _tf_seconds(timeframe)
            next_close = datetime.fromtimestamp(((int(time.time()) // tf_secs) + 1) * tf_secs, tz=timezone.utc)
            blb.push(bot.name, "INFO", f"Live monitoring active ({live_mode}) — next {timeframe} candle closes ~{next_close.strftime('%H:%M')} UTC")
            engine.set_runtime(bot.name, "live", f"Waiting for next {timeframe} candle close", mode=live_mode, next_close=next_close.isoformat())
        except Exception:
            blb.push(bot.name, "INFO", f"Live monitoring active ({live_mode}) — waiting for the next {timeframe} candle close")
            engine.set_runtime(bot.name, "live", f"Waiting for next {timeframe} candle close", mode=live_mode)

    except StoppedByUser:
        db.rollback()
        if run_backtest:
            # Trades committed by the aborted run would otherwise linger
            # as a half backtest in the analytics
            flush_backtest_data(engine, db, _log_name)
        if engine._run_tokens.get(bot_id) is _run_token:
            blb.push(_log_name, "INFO", "Stopped by user — startup aborted.")
            engine.clear_runtime(_log_name)
    except Exception as e:
        logger.error("Backfill Error: %s", e, exc_info=True)
        blb.push(_log_name, "ERROR", f"Backfill error: {e}")
        db.rollback()
        try:
            bot = db.query(BotConfig).filter(BotConfig.id == bot_id).first()
            if bot and bot.is_active:
                engine._engine_stop(bot, db, f"Startup error: {str(e)[:160]}")
                db.commit()
        except Exception:
            db.rollback()
    finally:
        # A superseded thread (restart) must not unmask the bot while the
        # replacement thread is still backfilling
        if engine._run_tokens.get(bot_id) is _run_token:
            engine._backfilling_bots.discard(_log_name)
        db.close()
