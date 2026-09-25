"""Exchange-side plumbing for paper/live execution: client construction,
order reconciliation, exchange minimums, fee normalization and wallet reads.
Everything here talks to a ccxt instance and nothing here touches engine
state, so tests swap the instance for a fake at `BotManager._get_ccxt_instance`."""
import logging
import threading
import time
from backend.models.exchange_keys import ExchangeKey
from backend.models.positions import Position
from backend.core.exchange_registry import get_authenticated_exchange, market_caps
from backend.core import bot_log_buffer as blb
from backend.engine.contracts import spec_for_instance
from backend.engine.symbols import base_of, cash_currency, is_derivative, normalize

logger = logging.getLogger("apexalgo.bot_manager")


def get_ccxt_instance(api_key_record: ExchangeKey, symbol: str | None = None):
    """Shared, market-loaded instance from the registry (1 h TTL). `symbol`
    picks the ccxt class for the contract kind on exchanges that serve
    inverse contracts from a separate class (binance -> binancecoinm)."""
    return get_authenticated_exchange(api_key_record, symbol=symbol)

def reconcile_order(ccxt_inst, order, ccxt_symbol, attempts=5, delay=1.0):
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

def cancel_unfilled_order(ccxt_inst, order_id, ccxt_symbol):
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

def contract_size(ccxt_inst, ccxt_symbol) -> float:
    """Units per contract from the market (1 for spot and for most linear
    perpetuals; e.g. 0.001 BTC on kucoinfutures, 100 USD on BTC/USD:BTC).
    Unknown metadata counts as 1."""
    if not is_derivative(ccxt_symbol):
        return 1.0
    return spec_for_instance(ccxt_inst, ccxt_symbol).contract_size


def to_contracts(ccxt_inst, ccxt_symbol, qty) -> float:
    """Position size (base units on spot/linear, contracts on inverse) → the
    `amount` ccxt expects in create_order. Identity on spot and inverse."""
    return spec_for_instance(ccxt_inst, ccxt_symbol).to_contracts(qty)


def from_contracts(ccxt_inst, ccxt_symbol, contracts) -> float:
    return spec_for_instance(ccxt_inst, ccxt_symbol).from_contracts(contracts)


def below_market_minimum(ccxt_inst, ccxt_symbol, amount, price):
    """Return a human-readable violation string when an order would fall
    below the exchange's minimum amount/cost limits, else None. Missing
    limit metadata is treated as no restriction. `amount` is the position
    size (base units on spot/linear, contracts on inverse); the exchange's
    amount limit is in contracts and its cost limit in the quote currency."""
    try:
        spec = spec_for_instance(ccxt_inst, ccxt_symbol)
        limits = (ccxt_inst.market(ccxt_symbol) or {}).get("limits") or {}
        min_amount = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        if min_amount is not None:
            contracts = spec.to_contracts(amount)
            if contracts < float(min_amount):
                unit = " contracts" if contracts != amount or spec.is_inverse else ""
                return f"amount {contracts:g}{unit} below exchange minimum {float(min_amount)}{unit}"
        if min_cost is not None and price:
            order_value = spec.notional_quote(amount, float(price))
            if order_value < float(min_cost):
                return f"order {order_value:.2f} {spec.quote} below exchange minimum {float(min_cost):.2f} {spec.quote}"
    except Exception:
        return None
    return None

def fee_cash(fee_info, ccxt_symbol, price, ccxt_inst=None):
    """CCXT fee cost converted to the pair's cash currency (quote on spot,
    settle on derivatives) so it can be netted against PnL. Base-denominated
    fees (typical for spot buys) use the fill price, quote-denominated fees
    on an inverse contract are divided by it; a third currency (BNB, KCS, MX
    discounts) is converted via `fetch_ticker("<ccy>/<cash>")` on `ccxt_inst`
    when given. When no rate can be found the fee is booked as 0 with a
    warning — never as if it were cash (a 0.001 BNB fee is not 0.001 USDT)."""
    if not fee_info:
        return 0.0
    try:
        cost = float(fee_info.get("cost", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if not cost:
        return 0.0
    currency = fee_info.get("currency")
    if not currency or '/' not in ccxt_symbol:
        return cost
    spec = spec_for_instance(ccxt_inst, ccxt_symbol) if ccxt_inst is not None else None
    if spec is None:
        from backend.engine.contracts import spec_from_symbol
        spec = spec_from_symbol(ccxt_symbol)
    converted = spec.fee_cash_from_fill(fee_info, price)
    if converted is not None:
        return converted
    ccy = str(currency).upper()
    cash = spec.cash_currency
    rate = None
    if ccxt_inst is not None:
        try:
            ticker = ccxt_inst.fetch_ticker(f"{ccy}/{cash}")
            rate = float(ticker.get("last") or ticker.get("close") or 0) or None
        except Exception as exc:
            logger.warning("fee_cash: no %s/%s rate for the fee on %s: %s", ccy, cash, ccxt_symbol, exc)
    if rate:
        return cost * rate
    logger.warning("fee_cash: fee of %s %s on %s could not be converted to %s — booked as 0", cost, ccy, ccxt_symbol, cash)
    return 0.0


# Pre-sprint-D name, kept for the BotManager seam and older callers
fee_in_quote = fee_cash

def wallet_held(balance: dict, token: str) -> float:
    """free + used of `token` from a ccxt fetch_balance() result."""
    v = balance.get(token)
    if isinstance(v, dict):
        return float(v.get("free") or 0) + float(v.get("used") or 0)
    return float((balance.get("free") or {}).get(token) or 0) + float((balance.get("used") or {}).get(token) or 0)

def reconcile_positions_with_wallet(db, bot, ccxt_inst, balance: dict, mode: str):
    """Before go-live, check that the exchange still holds what the open
    `mode` positions in the DB say it should. Returns a list of mismatch
    descriptions (empty when consistent). A position whose base balance is
    short by more than two precision steps means the books and the wallet
    have diverged (manual sell, transfer, another tool) — the bot must not
    manage exits it cannot fill."""
    open_pos = db.query(Position).filter(
        Position.bot_name == bot.name, Position.status == "open", Position.mode == mode,
    ).order_by(Position.id).all()
    if not open_pos:
        return []
    expected: dict = {}
    ids: dict = {}
    tol: dict = {}
    for pos in open_pos:
        sym = normalize(pos.symbol)
        base = base_of(sym)
        expected[base] = expected.get(base, 0.0) + float(pos.amount or 0)
        ids.setdefault(base, []).append(pos.id)
        step = 0.0
        try:
            prec = (ccxt_inst.market(sym).get("precision") or {}).get("amount")
            if prec is not None:
                prec = float(prec)
                step = prec if prec < 1 else 10.0 ** (-prec)
        except Exception:
            pass
        tol[base] = max(tol.get(base, 0.0), 2 * step)
    problems = []
    for base, want in expected.items():
        held = wallet_held(balance, base)
        if held + tol[base] + 1e-12 < want:
            problems.append(f"Position #{','.join(map(str, ids[base]))}: DB holds {want:g} {base} but exchange holds {held:g} {base}")
    return problems

def reconcile_positions_with_exchange(db, bot, ccxt_inst, mode: str):
    """Derivatives counterpart of `reconcile_positions_with_wallet`: a
    perpetual position holds no base coin, so compare the open `mode`
    positions in the DB against `fetch_positions()` on the exchange. Returns
    mismatch descriptions (empty when consistent): the exchange must hold at
    least the DB amount (within two precision steps) on the same side."""
    open_pos = db.query(Position).filter(
        Position.bot_name == bot.name, Position.status == "open", Position.mode == mode,
    ).order_by(Position.id).all()
    if not open_pos:
        return []
    expected: dict = {}
    ids: dict = {}
    tol: dict = {}
    for pos in open_pos:
        sym = normalize(pos.symbol)
        # Signed like the exchange side below: shorts count negative
        _signed = -float(pos.amount or 0) if (pos.side or "long") == "short" else float(pos.amount or 0)
        expected[sym] = expected.get(sym, 0.0) + _signed
        ids.setdefault(sym, []).append(pos.id)
        step = 0.0
        try:
            prec = (ccxt_inst.market(sym).get("precision") or {}).get("amount")
            if prec is not None:
                prec = float(prec)
                step = prec if prec < 1 else 10.0 ** (-prec)
        except Exception:
            pass
        # Tolerance in position units: two precision steps (contracts)
        tol[sym] = max(tol.get(sym, 0.0), spec_for_instance(ccxt_inst, sym).from_contracts(2 * step))
    held: dict = {}
    for p in ccxt_inst.fetch_positions(list(expected)) or []:
        sym = normalize(p.get("symbol"))
        if sym not in expected:
            continue
        contracts = float(p.get("contracts") or 0)
        if contracts <= 0:
            continue
        side = str(p.get("side") or "long").lower()
        qty = spec_for_instance(ccxt_inst, sym).from_contracts(contracts)
        held[sym] = held.get(sym, 0.0) + (qty if side == "long" else -qty)
    problems = []
    for sym, want in expected.items():
        have = held.get(sym, 0.0)
        tag = f"Position #{','.join(map(str, ids[sym]))}"
        if want < 0:
            # DB short: the exchange must hold at least as large a short
            if have > 0:
                problems.append(f"{tag}: DB holds a short of {-want:g} {sym} but the exchange holds a long")
            elif -have + tol[sym] + 1e-12 < -want:
                problems.append(f"{tag}: DB holds a short of {-want:g} {sym} but exchange position is {-have:g}")
        elif have < 0:
            problems.append(f"{tag}: DB holds a long of {want:g} {sym} but the exchange holds a short")
        elif have + tol[sym] + 1e-12 < want:
            problems.append(f"{tag}: DB holds {want:g} {sym} but exchange position is {have:g}")
    return problems


# (key name, symbol, leverage, margin mode) already applied on the exchange
# in this process — set_leverage is a real API call, do it once, not per tick
_leverage_applied: set = set()
_leverage_lock = threading.Lock()


def invalidate_leverage_cache(key_name=None):
    """Forget which (key, symbol, leverage, margin mode) combinations were
    confirmed on the exchange — called when a key is saved or deleted so a
    re-created key with the same name is set up again."""
    with _leverage_lock:
        if key_name is None:
            _leverage_applied.clear()
        else:
            for tag in [t for t in _leverage_applied if t[0] == str(key_name)]:
                _leverage_applied.discard(tag)


def exchange_position_gone(ccxt_inst, ccxt_symbol, side="long"):
    """True when `fetch_positions([symbol])` shows no open contracts on
    `side` — the position was liquidated or closed outside the bot. Raises
    when the exchange cannot be asked (the caller decides what to do)."""
    sym = normalize(ccxt_symbol)
    for p in ccxt_inst.fetch_positions([sym]) or []:
        if normalize(p.get("symbol")) != sym:
            continue
        if float(p.get("contracts") or 0) <= 0:
            continue
        if str(p.get("side") or "long").lower() == side:
            return False
    return True


def ensure_leverage(ccxt_inst, api_key_record, ccxt_symbol, leverage, margin_mode, bot_name):
    """Put the exchange's per-symbol leverage and margin mode in line with the
    bot before it trades a derivative. Exchanges without one of the calls
    (ccxt NotSupported) are skipped with an INFO line; 'leverage not
    modified' style answers are fine. Any other failure raises — a bot must
    not trade at an unknown leverage. No-op on spot."""
    if not is_derivative(ccxt_symbol):
        return
    lev = int(float(leverage or 1))
    mode = str(margin_mode or "isolated").lower()
    tag = (str(api_key_record.name), normalize(ccxt_symbol), lev, mode)
    with _leverage_lock:
        if tag in _leverage_applied:
            return
    caps = market_caps(api_key_record.exchange, "swap")
    import ccxt as _ccxt
    if getattr(ccxt_inst, "has", {}).get("setMarginMode"):
        try:
            ccxt_inst.set_margin_mode(mode, ccxt_symbol, params={"leverage": lev})
        except _ccxt.NotSupported:
            blb.push(bot_name, "INFO", f"{ccxt_symbol}: exchange does not support setting margin mode via API — set it to {mode} on the exchange")
        except Exception as exc:
            if not _benign_leverage_error(exc):
                raise RuntimeError(f"set_margin_mode({mode}) failed for {ccxt_symbol}: {exc}") from exc
    else:
        blb.push(bot_name, "INFO", f"{ccxt_symbol}: exchange does not support setting margin mode via API — set it to {mode} on the exchange")
    if caps is not None and caps.leverage_in_order:
        blb.push(bot_name, "INFO", f"{ccxt_symbol}: leverage {lev}x is sent with each order")
    elif getattr(ccxt_inst, "has", {}).get("setLeverage"):
        try:
            ccxt_inst.set_leverage(lev, ccxt_symbol, params={"marginMode": mode})
        except _ccxt.NotSupported:
            raise RuntimeError(f"Exchange cannot set leverage for {ccxt_symbol} via API")
        except Exception as exc:
            if not _benign_leverage_error(exc):
                raise RuntimeError(f"set_leverage({lev}) failed for {ccxt_symbol}: {exc}") from exc
    else:
        raise RuntimeError(f"Exchange cannot set leverage for {ccxt_symbol} via API")
    blb.push(bot_name, "INFO", f"{ccxt_symbol}: leverage {lev}x ({mode}) confirmed on the exchange")
    with _leverage_lock:
        _leverage_applied.add(tag)


def _benign_leverage_error(exc) -> bool:
    """'Already set' answers exchanges return as errors."""
    msg = str(exc).lower()
    return any(t in msg for t in ("not modified", "no need to change", "already", "same as", "110043", "59000"))


def get_live_capital(balance_cache, ccxt_inst, api_key_record, ccxt_symbol, bot_name, ttl=30):
    """Free balance of the pair's cash currency on the exchange (quote on
    spot — USDT, EUR, BTC —, settle on derivatives — USDT on linear, BTC on
    BTC/USD:BTC), cached briefly to spare rate limits. Returns None when the
    balance cannot be determined so the caller can fall back to the
    configured capital. `balance_cache` maps
    (key_name, cash_currency) -> (fetched_at, free)."""
    quote = cash_currency(ccxt_symbol)
    cache_key = (api_key_record.name, quote)
    now = time.monotonic()
    cached = balance_cache.get(cache_key)
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
            balance_cache[cache_key] = (now, free)
            return free
        logger.warning("No %s balance found for key '%s'", quote, api_key_record.name)
        blb.push(bot_name, "WARN", f"Could not read {quote} balance from exchange")
    except Exception as exc:
        logger.warning("fetch_balance failed for key '%s': %s", api_key_record.name, exc)
        blb.push(bot_name, "WARN", f"Balance fetch failed: {exc}")
    return None
