import logging
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
    directly from a decrypted ExchangeKey record.
    """
    from backend.core.encryption import decrypt_data
    return build_exchange(
        exchange_id=key_record.exchange,
        api_key=decrypt_data(key_record.api_key),
        api_secret=decrypt_data(key_record.api_secret),
        passphrase=decrypt_data(key_record.passphrase) if key_record.passphrase else None,
        sandbox=key_record.is_sandbox,
    )
