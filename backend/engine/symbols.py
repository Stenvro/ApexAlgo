"""Unified-symbol helpers shared by the engine and the routers.

ccxt symbols are ``BASE/QUOTE`` for spot and ``BASE/QUOTE:SETTLE`` for
derivatives (``BTC/USDT:USDT`` = the USDT-margined BTC perpetual). Every
place that used to split on ``/`` goes through here, so a derivative symbol
never yields ``"USDT:USDT"`` as its quote currency. For spot symbols the
results are exactly what the old ``split('/')`` produced.
"""

MARKET_TYPES = ("spot", "swap")
DEFAULT_MARKET_TYPE = "spot"
DEFAULT_MARGIN_MODE = "isolated"
MARGIN_MODES = ("isolated", "cross")


def normalize(symbol) -> str:
    """``btc-usdt`` / ``BTC-USDT:USDT`` → ``BTC/USDT`` / ``BTC/USDT:USDT``."""
    return str(symbol or "").replace('-', '/').upper()


def is_derivative(symbol) -> bool:
    """True for the ``BASE/QUOTE:SETTLE`` form."""
    return ':' in str(symbol or "")


def market_type_of(symbol) -> str:
    return "swap" if is_derivative(symbol) else "spot"


def base_of(symbol) -> str:
    return normalize(symbol).split('/')[0]


def quote_of(symbol) -> str:
    """Quote currency: ``BTC/USDT`` → ``USDT``, ``BTC/USDT:USDT`` → ``USDT``."""
    sym = normalize(symbol)
    rest = sym.split('/')[-1] if '/' in sym else sym
    return rest.split(':')[0]


def settle_of(symbol):
    """Settlement currency of a derivative symbol, None for spot."""
    sym = normalize(symbol)
    return sym.split(':')[-1] if ':' in sym else None


def cash_currency(symbol) -> str:
    """The currency a position is funded in and PnL is paid in: the settle
    currency for derivatives, the quote currency for spot."""
    return settle_of(symbol) or quote_of(symbol)


def market_type_for(settings: dict, key_record=None) -> str:
    """Market type a bot trades on. A linked API key is bound to one market
    type and wins (the validator refuses a mismatch on save); otherwise the
    bot's own setting, defaulting to spot for every pre-existing bot."""
    if key_record is not None:
        kt = getattr(key_record, "market_type", None)
        if kt:
            return str(kt).lower()
    return str((settings or {}).get("market_type") or DEFAULT_MARKET_TYPE).lower()


def leverage_for(settings: dict, market_type: str | None = None) -> float:
    """Configured leverage; always 1 on spot whatever the setting says."""
    mt = market_type or market_type_for(settings)
    if mt == "spot":
        return 1.0
    try:
        lev = float((settings or {}).get("leverage") or 1)
    except (TypeError, ValueError):
        lev = 1.0
    return lev if lev >= 1 else 1.0
