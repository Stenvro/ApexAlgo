import logging
import threading
import time
from dataclasses import dataclass, field
import ccxt

logger = logging.getLogger("apexalgo.exchange_registry")


@dataclass(frozen=True)
class MarketCaps:
    """What ApexAlgo may do on one market type of an exchange."""
    ccxt_id: str | None = None          # separate ccxt class for this market type (kucoinfutures)
    max_leverage: int = 1               # cap the validator/UI enforce (1 = no leverage)
    leverage_in_order: bool = False     # exchange takes leverage per order instead of set_leverage
    note: str | None = None             # shown in the key form / go-live checklist


SPOT = MarketCaps()


def _swap(**kw) -> MarketCaps:
    return MarketCaps(max_leverage=kw.pop("max_leverage", 10), **kw)


@dataclass(frozen=True)
class ExchangeSpec:
    """Everything ApexAlgo knows about one exchange, in one place.

    Adding an exchange = one entry in `EXCHANGES`; the routers, the key form
    and the builder derive their lists from here (via `GET /api/keys/exchanges`).
    ccxt itself is id-agnostic, so nothing engine-side needs to change.
    `markets` lists the market types a key/bot may use: every exchange has
    "spot"; "swap" (USDT/USDC-margined linear perpetuals, long-only until
    shorts land) only where ccxt can place, reduce and list positions.
    """
    name: str                                   # display name
    needs_passphrase: bool = False              # api_key + secret + passphrase
    ccxt_config: dict = field(default_factory=dict)  # extra ccxt constructor config
    keys_url: str | None = None                 # where to create API keys
    sandbox_note: str | None = None             # how to get testnet keys, if any
    candle_limit_note: str | None = None        # shown when the exchange caps OHLCV history
    markets: dict = field(default_factory=lambda: {"spot": SPOT})  # market type -> MarketCaps


# All exchanges supported by ApexAlgo, keyed by ccxt exchange ID.
EXCHANGES: dict[str, ExchangeSpec] = {
    "okx": ExchangeSpec(
        "OKX", needs_passphrase=True, ccxt_config={"hostname": "eea.okx.com"},
        keys_url="https://www.okx.com/account/my-api",
        sandbox_note="Demo trading keys are created under Trade → Demo trading → API.",
        markets={"spot": SPOT, "swap": _swap(note="Account must be in net (one-way) position mode.")}),
    "binance": ExchangeSpec(
        "Binance", keys_url="https://www.binance.com/en/my/settings/api-management",
        sandbox_note="Spot testnet keys come from testnet.binance.vision, futures testnet keys from testnet.binancefuture.com (separate accounts).",
        markets={"spot": SPOT, "swap": _swap(note="USDⓈ-M futures; account must be in one-way position mode.")}),
    "bitvavo": ExchangeSpec("Bitvavo", keys_url="https://account.bitvavo.com/user/api"),
    "coinbase": ExchangeSpec("Coinbase", keys_url="https://www.coinbase.com/settings/api"),
    "cryptocom": ExchangeSpec(
        "Crypto.com", keys_url="https://crypto.com/exchange/user/settings/api-management",
        sandbox_note="UAT sandbox keys are issued via the Crypto.com Exchange UAT environment."),
    "kraken": ExchangeSpec(
        "Kraken", keys_url="https://www.kraken.com/u/security/api",
        candle_limit_note="Kraken only serves its most recent 720 candles per timeframe",
        markets={"spot": SPOT, "swap": _swap(ccxt_id="krakenfutures", max_leverage=5,
                                             note="Kraken Futures uses its own API keys (futures.kraken.com); demo keys from demo-futures.kraken.com.")}),
    "kucoin": ExchangeSpec(
        "KuCoin", needs_passphrase=True, keys_url="https://www.kucoin.com/account/api",
        markets={"spot": SPOT, "swap": _swap(ccxt_id="kucoinfutures", leverage_in_order=True,
                                             note="KuCoin Futures uses its own API keys (Futures → API management).")}),
    "bybit": ExchangeSpec(
        "Bybit", keys_url="https://www.bybit.com/app/user/api-management",
        sandbox_note="Testnet keys are created on testnet.bybit.com (separate account).",
        markets={"spot": SPOT, "swap": _swap(note="Unified trading account, one-way position mode.")}),
    "gateio": ExchangeSpec(
        "Gate", keys_url="https://www.gate.io/myaccount/api_key_manage",
        sandbox_note="Testnet keys are created on testnet.gate.io (separate account).",
        markets={"spot": SPOT, "swap": _swap()}),
    "bitget": ExchangeSpec(
        "Bitget", needs_passphrase=True, keys_url="https://www.bitget.com/account/newapi",
        sandbox_note="Demo-trading keys are created under Demo Trading → API on bitget.com.",
        markets={"spot": SPOT, "swap": _swap(note="USDT-M futures, one-way position mode.")}),
    "mexc": ExchangeSpec(
        "MEXC", keys_url="https://www.mexc.com/user/openapi",
        sandbox_note="MEXC has no public testnet — keys can only be used for real trading."),
        # spot only: MEXC's futures order API is closed to the public ("under maintenance")
    "htx": ExchangeSpec(
        "HTX", keys_url="https://www.htx.com/en-us/apikey/",
        sandbox_note="HTX has no public testnet — keys can only be used for real trading.",
        markets={"spot": SPOT, "swap": _swap()}),
    "bingx": ExchangeSpec(
        "BingX", keys_url="https://bingx.com/en/account/api/",
        sandbox_note="VST demo-trading keys are created under Demo Trading → API on bingx.com.",
        markets={"spot": SPOT, "swap": _swap(note="Perpetual futures, one-way position mode.")}),
}

# Backwards-compatible views used by the routers: {id: display name} and the
# set of exchanges that authenticate with a passphrase.
SUPPORTED_EXCHANGES: dict[str, str] = {ex_id: spec.name for ex_id, spec in EXCHANGES.items()}
_PASSPHRASE_EXCHANGES: frozenset[str] = frozenset(ex_id for ex_id, spec in EXCHANGES.items() if spec.needs_passphrase)


def exchange_spec(exchange_id: str) -> ExchangeSpec | None:
    """Spec for an exchange id (case-insensitive); None when unsupported."""
    return EXCHANGES.get(str(exchange_id or "").lower())


def market_caps(exchange_id: str, market_type: str = "spot") -> MarketCaps | None:
    """Capabilities of one market type on an exchange; None when the exchange
    is unknown or does not offer that market type."""
    spec = exchange_spec(exchange_id)
    if spec is None:
        return None
    return spec.markets.get(str(market_type or "spot").lower())


def ccxt_id_for(exchange_id: str, market_type: str = "spot") -> str:
    """The ccxt class that serves this market type (kucoin swap = kucoinfutures)."""
    caps = market_caps(exchange_id, market_type)
    return (caps.ccxt_id if caps and caps.ccxt_id else str(exchange_id).lower())


_sandbox_cache: dict[tuple[str, str], bool] = {}


def exchange_has_sandbox(exchange_id: str, market_type: str = "spot") -> bool:
    """Whether ccxt can put this exchange (market type) in sandbox/testnet
    mode. Probed on a throwaway instance (no network): some exchanges use a
    test URL, others a demo-trading header (Bitget), and ccxt raises for the
    rest."""
    exchange_id = str(exchange_id or "").lower()
    market_type = str(market_type or "spot").lower()
    key = (exchange_id, market_type)
    if key not in _sandbox_cache:
        ok = False
        try:
            if market_caps(exchange_id, market_type) is not None:
                getattr(ccxt, ccxt_id_for(exchange_id, market_type))().set_sandbox_mode(True)
                ok = True
        except Exception:
            pass
        _sandbox_cache[key] = ok
    return _sandbox_cache[key]


def build_exchange(
    exchange_id: str,
    api_key: str | None = None,
    api_secret: str | None = None,
    passphrase: str | None = None,
    sandbox: bool = False,
    market_type: str = "spot",
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
        Only applied for exchanges whose spec has `needs_passphrase`.
    sandbox : bool
        Enable exchange sandbox / testnet mode when supported.
    market_type : str
        "spot" (default, unchanged behaviour) or "swap": sets ccxt's
        `defaultType` and picks the derivatives class where the exchange has
        one. Raises ValueError when the spec has no such market type.
    """
    exchange_id = exchange_id.lower()
    market_type = str(market_type or "spot").lower()

    if exchange_id not in SUPPORTED_EXCHANGES:
        raise ValueError(
            f"Exchange '{exchange_id}' is not supported. "
            f"Supported: {', '.join(SUPPORTED_EXCHANGES)}"
        )
    spec = EXCHANGES[exchange_id]
    caps = spec.markets.get(market_type)
    if caps is None:
        raise ValueError(
            f"Exchange '{exchange_id}' has no '{market_type}' market in ApexAlgo. "
            f"Available: {', '.join(spec.markets)}"
        )

    cls = getattr(ccxt, ccxt_id_for(exchange_id, market_type), None)
    if cls is None:
        raise RuntimeError(
            f"Exchange '{exchange_id}' was not found in the installed CCXT version. "
            "Run: pip install --upgrade ccxt"
        )

    config: dict = {"enableRateLimit": True}
    config.update(spec.ccxt_config)
    # Pin ccxt's defaultType so balance/position calls address the right
    # account: ccxt's own default is not "spot" everywhere (bybit → swap)
    config["options"] = {**config.get("options", {}), "defaultType": market_type}

    if api_key:
        config["apiKey"] = api_key
        config["secret"] = api_secret

    if passphrase and spec.needs_passphrase:
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


def key_market_type(key_record) -> str:
    """Market type an ExchangeKey record is bound to (pre-phase-2 rows: spot)."""
    return str(getattr(key_record, "market_type", None) or "spot").lower()


def build_exchange_for_symbol(exchange_id: str, symbol: str) -> ccxt.Exchange:
    """Public (unauthenticated) instance for market data on `symbol`: the
    unified symbol says which market it lives on (``BTC/USDT:USDT`` → swap),
    so candle/ticker callers need no market-type plumbing of their own."""
    from backend.engine.symbols import market_type_of
    return build_exchange(exchange_id, market_type=market_type_of(symbol))


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


# Active symbols per (exchange, market type), refreshed after _MARKETS_TTL
# seconds so a newly listed pair shows up without a restart
_markets_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
_MARKETS_TTL = 3600


def get_exchange_symbols(exchange_id: str, market_type: str = "spot") -> list[str]:
    """Sorted list of tradeable symbols on the exchange: spot pairs
    ("BTC/USDT") or, for `market_type="swap"`, active linear (stablecoin-
    settled) perpetuals ("BTC/USDT:USDT"). Inverse/coin-margined contracts
    and dated futures are left out.

    Empty when the exchange cannot be reached — callers must treat that as
    "unknown", not "nothing is tradeable"."""
    exchange_id = exchange_id.lower()
    market_type = str(market_type or "spot").lower()
    cache_key = (exchange_id, market_type)
    hit = _markets_cache.get(cache_key)
    if hit and time.monotonic() - hit[0] < _MARKETS_TTL:
        return hit[1]
    try:
        exchange = build_exchange(exchange_id, market_type=market_type)
        markets = exchange.load_markets()
        if market_type == "spot":
            symbols = sorted(
                sym for sym, m in markets.items()
                if m.get("spot", True) and m.get("active", True) is not False
            )
        else:
            symbols = sorted(
                sym for sym, m in markets.items()
                if m.get("type") == market_type and m.get("linear")
                and m.get("active", True) is not False
            )
    except Exception as exc:
        logger.warning("Failed to load %s markets for '%s': %s", market_type, exchange_id, exc)
        return hit[1] if hit else []
    _markets_cache[cache_key] = (time.monotonic(), symbols)
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
        market_type=key_market_type(key_record),
    )


# Authenticated instances shared by the engine and the routers, keyed by
# (exchange, key name, sandbox, market type). Building one per bot per candle
# re-decrypted the credentials and re-downloaded the market list every tick;
# here markets are loaded once and the instance is rebuilt after _AUTH_TTL
# seconds so a stale session or rate-limit bookkeeping never sticks around
# for good. Sync ccxt instances serialize nothing themselves; order paths are
# already serialized per bot, and concurrent REST reads on one instance are safe.
_auth_cache: dict[tuple[str, str, bool, str], tuple[float, ccxt.Exchange]] = {}
_auth_lock = threading.Lock()
_AUTH_TTL = 3600


def _auth_key(key_record) -> tuple[str, str, bool, str]:
    return (str(key_record.exchange).lower(), str(key_record.name),
            bool(key_record.is_sandbox), key_market_type(key_record))


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
