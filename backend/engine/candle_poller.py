import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import pairwise

from sqlalchemy.orm import Session

from backend.core.database import SessionLocal
from backend.core.exchange_registry import build_exchange, get_exchange_timeframes
from backend.core.events import event_bus
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.exchange_keys import ExchangeKey

logger = logging.getLogger("apexalgo.poller")


class CandlePoller:
    """
    Exchange-agnostic market data engine.

    For every active bot subscription (exchange, symbol, timeframe) the poller:
      1. Back-fills missing historical candles on startup using CCXT fetch_ohlcv.
      2. Polls the exchange at a safe interval to detect newly closed candles.
      3. Saves closed candles to the database and publishes a CANDLE_CLOSED event
         so BotManager can evaluate strategy logic.

    This replaces the OKX-specific WebSocket streamer with a universal REST
    approach that works for any CCXT-supported exchange.
    """

    def __init__(self):
        self.running = False
        self.needs_reconnect = False
        self._poll_tasks: list[asyncio.Task] = []
        self._exchange_cache: dict[str, tuple[float, object, threading.Lock]] = {}  # exchange_id → (created_at, ccxt instance, lock)
        self._exchange_cache_ttl = 3600  # rebuild exchange instances after 1 hour
        # Last closed candle ts per (exchange, symbol, timeframe), kept across
        # reconnects so a restarted poll task does not re-publish the same candle.
        self._last_closed_ts: dict[tuple, int] = {}
        # First candle an exchange serves per (exchange, symbol, timeframe), so
        # a restart does not probe the listing date again
        self._listing_start: dict[tuple, int] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self):
        self.running = True
        logger.info("Candle Poller starting.")
        asyncio.create_task(self._listen_for_bot_changes())

        while self.running:
            self.needs_reconnect = False
            # Debounce: starting several bots at once fires a burst of
            # BOT_STATE_CHANGED events — let the burst settle so all new
            # subscriptions land in one backfill cycle instead of queueing
            # behind each other.
            await asyncio.sleep(1.5)
            subs = self._get_active_subscriptions()

            if not subs:
                await asyncio.sleep(2)
                continue

            await asyncio.to_thread(self._backfill_all, subs)

            # Cancel any stale polling tasks before starting fresh ones
            for task in self._poll_tasks:
                task.cancel()
            self._poll_tasks.clear()

            for (exchange_name, symbol, timeframe) in subs:
                task = asyncio.create_task(
                    self._poll_symbol(exchange_name, symbol, timeframe)
                )
                self._poll_tasks.append(task)

            logger.info("Poller live: %d subscription(s).", len(subs))

            while self.running and not self.needs_reconnect:
                await asyncio.sleep(1)

        for task in self._poll_tasks:
            task.cancel()

    def stop(self):
        self.running = False
        logger.info("Candle Poller stopped.")

    # ─────────────────────────────────────────────────────────────────────────
    # Subscription management
    # ─────────────────────────────────────────────────────────────────────────

    def _get_active_subscriptions(self) -> dict:
        """
        Returns { (exchange_name, symbol, timeframe): lookback_limit }
        for every active bot.

        Exchange resolution priority:
          1. Bot has an API key  → use that key's exchange.
          2. Bot settings contain data_exchange  → use that.
          3. Default → 'okx'.
        """
        db: Session = SessionLocal()
        subs: dict = {}
        try:
            active_bots = db.query(BotConfig).filter(BotConfig.is_active == True).all()
            all_keys = {k.name: k for k in db.query(ExchangeKey).all()}
            for bot in active_bots:
                settings = bot.settings or {}
                symbols = settings.get("symbols", [])
                if not symbols and settings.get("symbol"):
                    symbols = [settings.get("symbol")]

                timeframe = settings.get("timeframe")
                lookback = int(settings.get("backtest_lookback", 500))

                exchange_name = settings.get("data_exchange", "okx")
                api_key_name = settings.get("api_key_name")
                if api_key_name:
                    key_record = all_keys.get(api_key_name)
                    if key_record:
                        exchange_name = key_record.exchange

                if symbols and timeframe:
                    for symbol in symbols:
                        key = (exchange_name, symbol, timeframe)
                        subs[key] = max(subs.get(key, 0), lookback)
            return subs
        finally:
            db.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Historical back-fill
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_listing_start(exchange, symbol, timeframe, tf_ms, start_ts):
        """Return the timestamp (ms) of the oldest candle the exchange serves,
        walking back in 500-candle pages from the newest one. None if the pair
        has no candles at all. Stops once the page reaches ``start_ts``."""
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, limit=500)
        except Exception:
            return None
        if not batch:
            return None
        first_ts = int(batch[0][0])
        step = 500  # candles per backwards page; halved when the exchange serves a shorter window
        for _ in range(64):
            if first_ts <= start_ts or step < 1:
                break
            probe_since = max(first_ts - step * tf_ms, start_ts)
            time.sleep(0.35)
            try:
                older = exchange.fetch_ohlcv(symbol, timeframe, since=probe_since, limit=500)
            except Exception:
                break
            if not older or int(older[0][0]) >= first_ts:
                # Empty page: either the listing starts inside this window
                # (OKX only serves ~300 candles after `since`) or we are past
                # the listing date — narrow the step and try again
                if probe_since <= start_ts and not older:
                    break
                step //= 2
                continue
            first_ts = int(older[0][0])
        return first_ts

    @staticmethod
    def _repair_gaps(db, exchange, exchange_name, symbol, timeframe, tf_ms, start_ts, now_ms, max_gaps=25) -> int:
        """Scan the stored range for missing candles and refetch each hole once.
        Exchanges occasionally return short pages or skip candles around
        maintenance windows; a strategy evaluated over holes sees wrong
        indicator values. Returns the number of candles added."""
        start_dt = datetime.fromtimestamp(start_ts / 1000.0, tz=timezone.utc)
        rows = db.query(Candle.timestamp).filter(
            Candle.exchange == exchange_name,
            Candle.symbol == symbol,
            Candle.timeframe == timeframe,
            Candle.timestamp >= start_dt,
        ).order_by(Candle.timestamp.asc()).all()
        stamps = [int((r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0]).timestamp() * 1000) for r in rows]
        if len(stamps) < 2:
            return 0
        gaps = [(a, b) for a, b in pairwise(stamps) if b - a > tf_ms]
        if not gaps:
            return 0
        missing = sum((b - a) // tf_ms - 1 for a, b in gaps)
        logger.info("Back-fill: %s/%s/%s — %d gap(s), %d candles missing; refetching.",
                    exchange_name, symbol, timeframe, len(gaps), missing)
        added = 0
        for a, b in gaps[:max_gaps]:
            since = a + tf_ms
            while since < b:
                time.sleep(0.35)
                try:
                    batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
                except Exception as exc:
                    logger.warning("Back-fill gap fetch failed %s/%s/%s at %s: %s", exchange_name, symbol, timeframe, since, exc)
                    break
                if not batch:
                    break
                have = {int((r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0]).timestamp() * 1000)
                        for r in db.query(Candle.timestamp).filter(
                            Candle.exchange == exchange_name, Candle.symbol == symbol, Candle.timeframe == timeframe,
                            Candle.timestamp >= datetime.fromtimestamp(since / 1000.0, tz=timezone.utc),
                            Candle.timestamp < datetime.fromtimestamp(b / 1000.0, tz=timezone.utc)).all()}
                new = []
                for c in batch:
                    ts = int(c[0])
                    if ts >= b or ts < since or ts in have or ts + tf_ms > now_ms:
                        continue
                    new.append(Candle(exchange=exchange_name, symbol=symbol, timeframe=timeframe,
                                      timestamp=datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc),
                                      open=float(c[1]), high=float(c[2]), low=float(c[3]), close=float(c[4]),
                                      volume=float(c[5]), marketcap=0.0))
                if new:
                    db.bulk_save_objects(new)
                    db.commit()
                    added += len(new)
                last = int(batch[-1][0])
                if last + tf_ms >= b or len(batch) < 2:
                    break
                since = last + tf_ms
        still = missing - added
        if still > 0:
            logger.warning("Back-fill: %s/%s/%s — %d candle(s) still missing after gap repair (exchange has no data there; "
                           "indicators bridge the hole).", exchange_name, symbol, timeframe, still)
        return added

    def _backfill_all(self, subs: dict):
        """Run back-fill for all subscriptions in parallel (one thread per symbol)."""
        tasks = list(subs.items())
        max_workers = min(len(tasks), 5)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._backfill_one_symbol,
                    exchange_name, symbol, timeframe, lookback
                ): (exchange_name, symbol, timeframe)
                for (exchange_name, symbol, timeframe), lookback in tasks
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.error("Back-fill failed for %s: %s", key, exc, exc_info=True)

    def _backfill_one_symbol(
        self,
        exchange_name: str,
        symbol: str,
        timeframe: str,
        lookback_limit: int,
    ):
        # Fresh instance per thread to avoid shared rate-limit state
        exchange = build_exchange(exchange_name)

        # Validate timeframe before attempting fetch
        try:
            exchange.load_markets()
        except Exception:
            pass
        if exchange.timeframes and timeframe not in exchange.timeframes:
            supported = ', '.join(sorted(exchange.timeframes.keys()))
            logger.warning(
                "Back-fill skipped: %s does not support timeframe '%s'. Supported: %s",
                exchange_name, timeframe, supported,
            )
            return

        logger.info(
            "Back-fill: %s/%s/%s — requesting up to %d candles.",
            exchange_name, symbol, timeframe, lookback_limit,
        )

        try:
            tf_seconds = exchange.parse_timeframe(timeframe)
        except Exception:
            tf_seconds = 60

        tf_ms = tf_seconds * 1000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ts = now_ms - (lookback_limit * tf_ms)
        known_start = self._listing_start.get((exchange_name, symbol, timeframe))
        if known_start and known_start > start_ts:
            start_ts = known_start  # pair is younger than the lookback; skip the empty range
        current_since = start_ts
        total_saved = 0
        first_seen_ts = None  # oldest candle the exchange returned in this run

        db: Session = SessionLocal()
        try:
            # Resume from the newest stored candle instead of re-fetching the
            # whole lookback window on every reconnect. When the requested
            # lookback reaches further back than the stored history (a new
            # bot with a larger lookback), fill that older gap first, then
            # jump forward past the stored range.
            backward_until_ms = None
            resume_since = None
            oldest_ms = None
            last_existing = db.query(Candle.timestamp).filter(
                Candle.exchange == exchange_name,
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
            ).order_by(Candle.timestamp.desc()).first()
            if last_existing:
                last_dt = last_existing[0]
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                resume_since = int(last_dt.timestamp() * 1000) + tf_ms

                oldest_existing = db.query(Candle.timestamp).filter(
                    Candle.exchange == exchange_name,
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                ).order_by(Candle.timestamp.asc()).first()
                oldest_dt = oldest_existing[0]
                if oldest_dt.tzinfo is None:
                    oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
                oldest_ms = int(oldest_dt.timestamp() * 1000)

                if start_ts < oldest_ms - tf_ms:
                    backward_until_ms = oldest_ms
                elif resume_since > current_since:
                    current_since = resume_since

            while total_saved < lookback_limit:
                batch = None
                for attempt in range(3):
                    try:
                        batch = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=500)
                        break
                    except Exception as exc:
                        if attempt < 2:
                            logger.warning("Back-fill fetch error %s/%s/%s (attempt %d/3): %s", exchange_name, symbol, timeframe, attempt + 1, exc)
                            time.sleep(1 * (attempt + 1))
                        else:
                            logger.warning("Back-fill fetch failed after 3 attempts %s/%s/%s: %s", exchange_name, symbol, timeframe, exc)

                if not batch:
                    if total_saved == 0 and current_since == start_ts:
                        # First fetch returned empty — the pair may not have data that far back.
                        # Step forward in large jumps to find where data actually begins.
                        found_start = None
                        probe_since = start_ts
                        jump = (now_ms - start_ts) // 4  # quarter-jumps toward present
                        while probe_since < now_ms:
                            probe_since += jump
                            try:
                                probe = exchange.fetch_ohlcv(symbol, timeframe, since=probe_since, limit=10)
                            except Exception:
                                probe = None
                            time.sleep(0.35)
                            if probe:
                                found_start = int(probe[0][0])
                                break
                        if not found_start:
                            # Some exchanges (OKX) return nothing for a `since`
                            # far before the pair's listing instead of clamping
                            # to the first candle, so the jumps can miss a young
                            # pair entirely. Walk backwards from the newest
                            # candles instead until the exchange runs dry.
                            found_start = self._find_listing_start(exchange, symbol, timeframe, tf_ms, start_ts)
                        if found_start:
                            logger.info("Back-fill: %s/%s/%s — no data at requested start, found data from %s.",
                                exchange_name, symbol, timeframe,
                                datetime.fromtimestamp(found_start / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d'))
                            self._listing_start[(exchange_name, symbol, timeframe)] = found_start
                            current_since = found_start
                            continue
                        else:
                            break
                    else:
                        break

                # Deduplicate against DB for this batch's time range
                batch_start = datetime.fromtimestamp(int(batch[0][0]) / 1000.0, tz=timezone.utc)
                batch_end = datetime.fromtimestamp(int(batch[-1][0]) / 1000.0, tz=timezone.utc)
                if first_seen_ts is None or int(batch[0][0]) < first_seen_ts:
                    first_seen_ts = int(batch[0][0])

                existing_times = {
                    (r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0])
                    for r in db.query(Candle.timestamp).filter(
                        Candle.exchange == exchange_name,
                        Candle.symbol == symbol,
                        Candle.timeframe == timeframe,
                        Candle.timestamp >= batch_start,
                        Candle.timestamp <= batch_end,
                    ).all()
                }

                new_candles = []
                for c in batch:
                    # The final candle of a batch may still be forming; only fully
                    # closed candles may be stored, the live poller picks up the rest.
                    if int(c[0]) + tf_ms > now_ms:
                        continue
                    dt = datetime.fromtimestamp(int(c[0]) / 1000.0, tz=timezone.utc)
                    if dt not in existing_times:
                        try:
                            new_candles.append(Candle(
                                exchange=exchange_name,
                                symbol=symbol,
                                timeframe=timeframe,
                                timestamp=dt,
                                open=float(c[1]),
                                high=float(c[2]),
                                low=float(c[3]),
                                close=float(c[4]),
                                volume=float(c[5]),
                                marketcap=0.0,
                            ))
                        except (IndexError, ValueError) as exc:
                            logger.warning("Malformed candle skipped for %s: %s", symbol, exc)

                if new_candles:
                    db.bulk_save_objects(new_candles)
                    db.commit()
                    total_saved += len(new_candles)

                last_ts = int(batch[-1][0])

                # Older gap closed — skip past the already-stored range and
                # continue where the previous backfill left off
                if backward_until_ms is not None and last_ts + tf_ms >= backward_until_ms:
                    backward_until_ms = None
                    if resume_since and resume_since > last_ts:
                        current_since = resume_since
                        time.sleep(0.35)
                        continue

                if last_ts >= now_ms or len(batch) < 2:
                    break

                current_since = last_ts + 1
                time.sleep(0.35)

            total_saved += self._repair_gaps(db, exchange, exchange_name, symbol, timeframe, tf_ms, start_ts, now_ms)

            # Less history than requested: either the pair is younger than the
            # lookback or the exchange caps its OHLC history (Kraken: last 720
            # candles per timeframe, regardless of `since`). Say so once so a
            # short backtest is never a silent surprise.
            requested_start = now_ms - (lookback_limit * tf_ms)
            oldest_have = min([t for t in (first_seen_ts, oldest_ms) if t is not None], default=None)
            if oldest_have is not None and oldest_have > requested_start + 2 * tf_ms and known_start != oldest_have:
                self._listing_start[(exchange_name, symbol, timeframe)] = oldest_have
                available = (now_ms - oldest_have) // tf_ms
                cap_note = " (Kraken only serves its most recent 720 candles per timeframe)" if exchange_name == "kraken" else ""
                logger.warning(
                    "Back-fill: %s/%s/%s — exchange has no data before %s%s; %d of the requested %d candles are available. "
                    "Use a larger timeframe or another data exchange for a longer backtest.",
                    exchange_name, symbol, timeframe,
                    datetime.fromtimestamp(oldest_have / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d'),
                    cap_note, available, lookback_limit,
                )

            if total_saved > 0:
                logger.info(
                    "Back-fill complete: %s/%s/%s — saved %d new candles.",
                    exchange_name, symbol, timeframe, total_saved,
                )
            else:
                logger.info(
                    "Back-fill: %s/%s/%s — already up to date.",
                    exchange_name, symbol, timeframe,
                )
        except Exception as exc:
            db.rollback()
            logger.error("Back-fill DB error for %s/%s/%s: %s", exchange_name, symbol, timeframe, exc)
        finally:
            db.close()

        # Signal that backfill is done for this subscription
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                asyncio.ensure_future,
                event_bus.publish("BACKFILL_COMPLETE", {
                    "exchange": exchange_name,
                    "symbol": symbol,
                    "timeframe": timeframe,
                })
            )
        except RuntimeError:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Live polling
    # ─────────────────────────────────────────────────────────────────────────

    async def _poll_symbol(self, exchange_name: str, symbol: str, timeframe: str):
        """
        Poll for closed candles on a single (exchange, symbol, timeframe).

        The penultimate candle returned by fetch_ohlcv(limit=2) is always the
        most recently CLOSED candle. When its timestamp advances, we have a new
        closed candle.

        Scheduling: once a closed candle has been seen on an epoch-aligned
        boundary, the loop sleeps until the next boundary (+ a short grace)
        and then polls every few seconds until the exchange publishes the
        candle. All pairs of a bot therefore tick within seconds of the close
        instead of anywhere inside a free-running interval. Timeframes that
        are not epoch-aligned (1w/1M) fall back to a fixed interval of
        max(10s, min(60s, tf_seconds / 4)).
        """
        exchange, ex_lock = self._get_public_exchange(exchange_name)

        # Validate timeframe
        def _load_markets():
            with ex_lock:
                exchange.load_markets()

        try:
            await asyncio.to_thread(_load_markets)
        except Exception:
            pass
        if exchange.timeframes and timeframe not in exchange.timeframes:
            logger.warning("Poll skipped: %s does not support timeframe '%s'.", exchange_name, timeframe)
            return
        try:
            tf_seconds = exchange.parse_timeframe(timeframe)
        except Exception:
            tf_seconds = 60

        poll_interval = max(10, min(60, tf_seconds // 4))
        sub_key = (exchange_name, symbol, timeframe)
        last_closed_ts: int | None = self._last_closed_ts.get(sub_key)

        logger.info(
            "Poll active: %s/%s/%s every %ds.",
            exchange_name, symbol, timeframe, poll_interval,
        )

        tf_ms = tf_seconds * 1000
        boundary_grace = 2      # seconds after the close before the first poll
        retry_interval = 5      # seconds between polls while the candle is pending

        def _next_sleep(found_new: bool) -> float:
            """Seconds to sleep before the next poll."""
            now = time.time()
            aligned = last_closed_ts is not None and (last_closed_ts // 1000) % tf_seconds == 0
            if not aligned:
                return poll_interval
            next_boundary = (int(now) // tf_seconds + 1) * tf_seconds
            # The candle that should be closed by now; if it hasn't been seen
            # yet the exchange is still publishing it — poll again shortly
            expected_ts = (next_boundary - tf_seconds) * 1000
            if not found_new and last_closed_ts < expected_ts:
                # Back off to the regular interval if the exchange is minutes late
                return retry_interval if now - expected_ts / 1000 < 120 else poll_interval
            return max(1.0, next_boundary + boundary_grace - now)

        def _fetch(limit):
            # Re-resolve from the cache every call so the TTL rebuild takes effect,
            # and hold the per-exchange lock: sync CCXT instances are not thread-safe.
            inst, lock = self._get_public_exchange(exchange_name)
            with lock:
                return inst.fetch_ohlcv(symbol, timeframe, None, limit)

        while self.running and not self.needs_reconnect:
            try:
                # After downtime the gap can span multiple candles; widen the
                # fetch so none are missed
                fetch_limit = 2
                if last_closed_ts is not None:
                    now_ms = int(time.time() * 1000)
                    gap_ms = now_ms - last_closed_ts
                    if gap_ms > 2 * tf_ms:
                        fetch_limit = int(min(gap_ms // tf_ms + 2, 300))

                found_new = False
                candles = await asyncio.to_thread(_fetch, fetch_limit)
                if len(candles) >= 2:
                    # Every candle except the last (still forming) is closed
                    for closed in candles[:-1]:
                        closed_ts = int(closed[0])
                        if last_closed_ts is not None and closed_ts <= last_closed_ts:
                            continue
                        await self._save_and_notify(exchange_name, symbol, timeframe, closed)
                        last_closed_ts = closed_ts
                        self._last_closed_ts[sub_key] = closed_ts
                        found_new = True
                sleep_for = _next_sleep(found_new)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Poll error %s/%s/%s: %s", exchange_name, symbol, timeframe, exc
                )
                sleep_for = poll_interval

            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def _save_and_notify(
        self,
        exchange_name: str,
        symbol: str,
        timeframe: str,
        candle_data: list,
    ):
        ts_ms = int(candle_data[0])
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        def db_op():
            db: Session = SessionLocal()
            try:
                existing = db.query(Candle).filter(
                    Candle.exchange == exchange_name,
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.timestamp == dt,
                ).first()
                if not existing:
                    candle = Candle(
                        exchange=exchange_name,
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=dt,
                        open=float(candle_data[1]),
                        high=float(candle_data[2]),
                        low=float(candle_data[3]),
                        close=float(candle_data[4]),
                        volume=float(candle_data[5]),
                        marketcap=0.0,
                    )
                    db.add(candle)
                else:
                    # Update in case the candle was still forming when first saved
                    existing.open   = float(candle_data[1])
                    existing.high   = float(candle_data[2])
                    existing.low    = float(candle_data[3])
                    existing.close  = float(candle_data[4])
                    existing.volume = float(candle_data[5])
                db.commit()
                return True
            except Exception as exc:
                db.rollback()
                logger.warning("Failed to save candle %s/%s: %s", symbol, timeframe, exc)
                return False
            finally:
                db.close()

        saved = await asyncio.to_thread(db_op)
        if not saved:
            return
        await event_bus.publish("CANDLE_CLOSED", {
            "exchange":  exchange_name,
            "symbol":    symbol,
            "timeframe": timeframe,
            "timestamp": dt,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # Bot change listener
    # ─────────────────────────────────────────────────────────────────────────

    async def _listen_for_bot_changes(self):
        queue = event_bus.subscribe("BOT_STATE_CHANGED")
        while self.running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                logger.info(
                    "Bot state changed (id=%s, action=%s). Refreshing subscriptions.",
                    event["bot_id"], event["action"],
                )
                self.needs_reconnect = True
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_public_exchange(self, exchange_name: str):
        """Return (instance, lock) for a cached unauthenticated exchange used for
        public market data. Callers must hold the lock around every fetch since
        sync CCXT instances are not thread-safe."""
        now = time.monotonic()
        cached = self._exchange_cache.get(exchange_name)
        if cached and (now - cached[0]) < self._exchange_cache_ttl:
            return cached[1], cached[2]
        instance = build_exchange(exchange_name)
        # Keep the existing lock across rebuilds so in-flight fetches stay serialized
        lock = cached[2] if cached else threading.Lock()
        self._exchange_cache[exchange_name] = (now, instance, lock)
        return instance, lock


candle_poller = CandlePoller()
