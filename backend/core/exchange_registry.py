import logging
import threading
import time
import ccxt

logger = logging.getLogger("apexalgo.exchange_registry")

# All exchanges supported by ApexAlgo. Key = ccxt exchange ID, Value = display name.
SUPPORTED_EXCHANGES: dict[str, str] = {
    "okx":       "OKX",
    "binance":   "Binance",
    "bitvavo":   "Bitvavo",
    "coinbase":  "Coinbase",
    "cryptocom": "Crypto.com",
    "kraken":    "Kraken",
    "kucoin":    "KuCoin",
}

# Exchanges that authenticate via api_key + api_secret + passphrase.
# All others use only api_key + api_secret.
_PASSPHRASE_EXCHANGES: frozenset[str] = frozenset({"okx", "kucoin"})

# Per-exchange static configuration applied at instantiation.
_EXCHANGE_CONFIG: dict[str, dict] = {
    "okx": {"hostname": "eea.okx.com"},
}


def build_exchange(
    exchange_id: str,
    api_key: str | None = None,
    api_secret: str | None = None,
    passphrase: str | None = None,
    sandbox: bool = False,
) -> ccxt.Exchange:
    """
    Instantiate and return a configured CCXT exchange.

    Parameters
    ----------
    exchange_id : str
        Lowercase exchange identifier (e.g. "okx", "binance").
    api_key / api_secret : str, optional
        Omit for unauthenticated (public market data) connections.
    passphrase : str, optional
        Only applied for exchanges that require it (OKX, KuCoin).
    sandbox : bool
        Enable exchange sandbox / testnet mode when supported.
    """
    exchange_id = exchange_id.lower()

    if exchange_id not in SUPPORTED_EXCHANGES:
        raise ValueError(
            f"Exchange '{exchange_id}' is not supported. "
            f"Supported: {', '.join(SUPPORTED_EXCHANGES)}"
        )

    cls = getattr(ccxt, exchange_id, None)
    if cls is None:
        raise RuntimeError(
            f"Exchange '{exchange_id}' was not found in the installed CCXT version. "
            "Run: pip install --upgrade ccxt"
        )

    config: dict = {"enableRateLimit": True}
    config.update(_EXCHANGE_CONFIG.get(exchange_id, {}))

    if api_key:
        config["apiKey"] = api_key
        config["secret"] = api_secret

    if passphrase and exchange_id in _PASSPHRASE_EXCHANGES:
        config["password"] = passphrase

    exchange = cls(config)

    if sandbox:
        try:
            exchange.set_sandbox_mode(True)
        except Exception as exc:
            raise ValueError(
                f"Exchange '{exchange_id}' has no sandbox/testnet — "
                "cannot use this key in sandbox mode"
            ) from exc

    return exchange


# Cache for exchange timeframes (loaded once per exchange, reused)
_timeframe_cache: dict[str, dict[str, str]] = {}


def get_exchange_timeframes(exchange_id: str) -> dict[str, str]:
    """Return {timeframe_key: label} for the exchange. Cached after first call."""
    exchange_id = exchange_id.lower()
    if exchange_id in _timeframe_cache:
        return _timeframe_cache[exchange_id]

    try:
        exchange = build_exchange(exchange_id)
        exchange.load_markets()
        tf_map = dict(exchange.timeframes) if exchange.timeframes else {}
    except Exception as exc:
        logger.warning("Failed to load timeframes for '%s': %s", exchange_id, exc)
        tf_map = {}

    _timeframe_cache[exchange_id] = tf_map
    return tf_map


# Active spot symbols per exchange, refreshed after _MARKETS_TTL seconds so a
# newly listed pair shows up without a restart
_markets_cache: dict[str, tuple[float, list[str]]] = {}
_MARKETS_TTL = 3600


def get_exchange_symbols(exchange_id: str) -> list[str]:
    """Sorted list of tradeable spot symbols ("BTC/USDT") on the exchange.

    Empty when the exchange cannot be reached — callers must treat that as
    "unknown", not "nothing is tradeable"."""
    exchange_id = exchange_id.lower()
    hit = _markets_cache.get(exchange_id)
    if hit and time.monotonic() - hit[0] < _MARKETS_TTL:
        return hit[1]
    try:
        exchange = build_exchange(exchange_id)
        markets = exchange.load_markets()
        symbols = sorted(
            sym for sym, m in markets.items()
            if m.get("spot", True) and m.get("active", True) is not False
        )
    except Exception as exc:
        logger.warning("Failed to load markets for '%s': %s", exchange_id, exc)
        return hit[1] if hit else []
    _markets_cache[exchange_id] = (time.monotonic(), symbols)
    return symbols


def build_exchange_from_key(key_record) -> ccxt.Exchange:
    """
    Convenience wrapper: build an authenticated exchange instance
    directly from a decrypted ExchangeKey record. Always a fresh instance —
    use `get_authenticated_exchange` for the shared, cached one.
    """
    from backend.core.encryption import decrypt_data
    return build_exchange(
        exchange_id=key_record.exchange,
        api_key=decrypt_data(key_record.api_key),
        api_secret=decrypt_data(key_record.api_secret),
        passphrase=decrypt_data(key_record.passphrase) if key_record.passphrase else None,
        sandbox=key_record.is_sandbox,
    )


# Authenticated instances shared by the engine and the routers, keyed by
# (exchange, key name, sandbox). Building one per bot per candle re-decrypted
# the credentials and re-downloaded the market list every tick; here markets
# are loaded once and the instance is rebuilt after _AUTH_TTL seconds so a
# stale session or rate-limit bookkeeping never sticks around for good.
# Sync ccxt instances serialize nothing themselves; order paths are already
# serialized per bot, and concurrent REST reads on one instance are safe.
_auth_cache: dict[tuple[str, str, bool], tuple[float, ccxt.Exchange]] = {}
_auth_lock = threading.Lock()
_AUTH_TTL = 3600


def _auth_key(key_record) -> tuple[str, str, bool]:
    return (str(key_record.exchange).lower(), str(key_record.name), bool(key_record.is_sandbox))


def get_authenticated_exchange(key_record, *, load_markets: bool = True) -> ccxt.Exchange:
    """Cached authenticated instance for an ExchangeKey record (1 h TTL).
    Markets are loaded on first use, so callers' `load_markets()` is a no-op."""
    key = _auth_key(key_record)
    now = time.monotonic()
    with _auth_lock:
        hit = _auth_cache.get(key)
        cached = hit[1] if hit and now - hit[0] < _AUTH_TTL else None
    instance = cached or build_exchange_from_key(key_record)
    # A status poll may have built the instance without markets; the order
    # path needs them, so load lazily on whichever call asks first
    if load_markets and not instance.markets:
        instance.load_markets()
    if cached is None:
        with _auth_lock:
            _auth_cache[key] = (now, instance)
    return instance


def invalidate_authenticated_exchange(key_name: str | None = None) -> None:
    """Drop cached instances for a key (or all of them) — call after a key
    is saved, replaced or deleted so no bot keeps trading on old credentials."""
    with _auth_lock:
        for k in [k for k in _auth_cache if key_name is None or k[1] == key_name]:
            _auth_cache.pop(k, None)
