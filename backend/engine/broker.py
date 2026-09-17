"""Exchange-side plumbing for paper/live execution: client construction,
order reconciliation, exchange minimums, fee normalization and wallet reads.
Everything here talks to a ccxt instance and nothing here touches engine
state, so tests swap the instance for a fake at `BotManager._get_ccxt_instance`."""
import logging
import time
from backend.models.exchange_keys import ExchangeKey
from backend.models.positions import Position
from backend.core.exchange_registry import get_authenticated_exchange
from backend.core import bot_log_buffer as blb

logger = logging.getLogger("apexalgo.bot_manager")


def get_ccxt_instance(api_key_record: ExchangeKey):
    """Shared, market-loaded instance from the registry (1 h TTL)."""
    return get_authenticated_exchange(api_key_record)

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

def below_market_minimum(ccxt_inst, ccxt_symbol, amount, price):
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

def fee_in_quote(fee_info, ccxt_symbol, price):
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
        sym = str(pos.symbol).replace('-', '/').upper()
        base = sym.split('/')[0]
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

def get_live_capital(balance_cache, ccxt_inst, api_key_record, ccxt_symbol, bot_name, ttl=30):
    """Free quote-currency balance on the exchange, cached briefly to spare
    rate limits. Returns None when the balance cannot be determined so the
    caller can fall back to the configured capital. `balance_cache` maps
    (key_name, quote) -> (fetched_at, free)."""
    quote = ccxt_symbol.split('/')[-1]
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
