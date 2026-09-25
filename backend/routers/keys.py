import logging
import os
import threading
import time
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import ccxt

from backend.core.database import get_db
from backend.models.exchange_keys import ExchangeKey
from backend.models.bots import BotConfig
from backend.core.security import verify_api_key
from backend.core.encryption import encrypt_data
from backend.core.exchange_registry import (
    build_exchange, get_authenticated_exchange, invalidate_authenticated_exchange,
    exchange_has_sandbox, EXCHANGES, SUPPORTED_EXCHANGES, market_caps, key_market_type,
)
from backend.engine.symbols import DEFAULT_MARKET_TYPE, MARKET_TYPES
from backend.engine.broker import invalidate_leverage_cache

logger = logging.getLogger("apexalgo.keys")

router = APIRouter(
    prefix="/api/keys",
    tags=["Exchange Keys"],
    dependencies=[Depends(verify_api_key)]
)

class ExchangeKeyCreate(BaseModel):
    name: str
    exchange: str = "okx"
    api_key: str
    api_secret: str
    passphrase: str = ""
    is_sandbox: bool = True
    # A key is bound to one market: spot keys and swap keys are separate records
    market_type: str = DEFAULT_MARKET_TYPE


@router.get("/exchanges")
def list_exchanges():
    """Capabilities per supported exchange (from the registry spec) for the
    connection form, the data manager and the builder. `has_sandbox` is
    probed from ccxt so it tracks the installed version. `markets` lists the
    market types ApexAlgo can trade on the exchange with their limits."""
    out = []
    for ex_id, spec in EXCHANGES.items():
        out.append({
            "id": ex_id,
            "name": spec.name,
            "needs_passphrase": spec.needs_passphrase,
            "has_sandbox": exchange_has_sandbox(ex_id),
            "keys_url": spec.keys_url,
            "sandbox_note": spec.sandbox_note,
            "markets": {
                mt: {
                    "has_sandbox": exchange_has_sandbox(ex_id, mt),
                    "max_leverage": caps.max_leverage,
                    "leverage_in_order": caps.leverage_in_order,
                    "note": caps.note,
                }
                for mt, caps in spec.markets.items()
            },
        })
    return out


@router.post("")
def save_exchange_keys(req: ExchangeKeyCreate, db: Session = Depends(get_db)):
    exchange_id = req.exchange.lower()
    if exchange_id not in SUPPORTED_EXCHANGES:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange '{exchange_id}'.")
    market_type = (req.market_type or DEFAULT_MARKET_TYPE).strip().lower()
    if market_type not in MARKET_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid market_type '{req.market_type}'. Use one of: {', '.join(MARKET_TYPES)}.")
    if market_caps(exchange_id, market_type) is None:
        raise HTTPException(status_code=400, detail=f"{SUPPORTED_EXCHANGES[exchange_id]} has no '{market_type}' market in ApexAlgo.")
    # Bots keep trading the market their key was verified on: re-saving a
    # key under another market type is refused while bots still reference it
    existing = db.query(ExchangeKey).filter(ExchangeKey.name == req.name).first()
    if existing and (getattr(existing, "market_type", None) or DEFAULT_MARKET_TYPE) != market_type:
        linked = [b_name for b_name, b_settings in db.query(BotConfig.name, BotConfig.settings).all()
                  if (b_settings or {}).get("api_key_name") == req.name]
        if linked:
            raise HTTPException(status_code=409, detail=f"Key '{req.name}' is a {existing.market_type or DEFAULT_MARKET_TYPE} key linked to {', '.join(linked)}; save the {market_type} key under a new name.")
    try:
        test_exchange = build_exchange(
            exchange_id,
            api_key=req.api_key,
            api_secret=req.api_secret,
            passphrase=req.passphrase or None,
            sandbox=req.is_sandbox,
            market_type=market_type,
        )
        test_exchange.fetch_balance()
    except Exception as e:
        logger.warning("Exchange key validation failed for '%s': %s", req.name, type(e).__name__)
        raise HTTPException(status_code=400, detail="Connection rejected: Could not authenticate with the provided credentials.")

    try:
        enc_key = encrypt_data(req.api_key)
        enc_secret = encrypt_data(req.api_secret)
        enc_passphrase = encrypt_data(req.passphrase)

        if existing:
            existing.api_key = enc_key
            existing.api_secret = enc_secret
            existing.passphrase = enc_passphrase
            existing.is_sandbox = req.is_sandbox
            existing.exchange = req.exchange
            existing.market_type = market_type
        else:
            new_key = ExchangeKey(
                name=req.name,
                exchange=req.exchange,
                api_key=enc_key,
                api_secret=enc_secret,
                passphrase=enc_passphrase,
                is_sandbox=req.is_sandbox,
                market_type=market_type,
            )
            db.add(new_key)

        db.commit()
        invalidate_authenticated_exchange(req.name)  # replaced credentials must not linger in the registry
        invalidate_leverage_cache(req.name)  # leverage/margin mode must be confirmed again on the new key
        return {"message": f"Exchange key '{req.name}' verified and saved securely."}
    except Exception as e:
        logger.error("Database error saving key '%s': %s", req.name, e)
        raise HTTPException(status_code=500, detail="Database error while saving exchange key.")

def _humanize_exchange_error(exc: Exception) -> str:
    name = type(exc).__name__
    if isinstance(exc, ccxt.AuthenticationError):
        return "Authentication failed — the key, secret or passphrase is wrong, expired or lacks read permission."
    if isinstance(exc, ccxt.PermissionDenied):
        return "Permission denied — the key exists but is not allowed to read balances (check IP allowlist and permissions)."
    if isinstance(exc, ccxt.DDoSProtection) or isinstance(exc, ccxt.RateLimitExceeded):
        return "Rate limited by the exchange — try again in a moment."
    if isinstance(exc, ccxt.NetworkError):
        return "Network error — the exchange did not respond."
    if isinstance(exc, ValueError) and "sandbox" in str(exc).lower():
        return "This exchange has no sandbox/testnet — re-add the key as Live."
    return name


@router.get("")
def get_exchange_keys_status(db: Session = Depends(get_db)):
    keys = db.query(ExchangeKey).all()
    bots = db.query(BotConfig.name, BotConfig.is_active, BotConfig.settings).all()
    result = []
    for k in keys:
        is_active = False
        error_msg = ""
        latency_ms = None
        started = time.monotonic()
        try:
            # Status polls every few seconds: reuse the registry instance
            # instead of decrypting + instantiating every exchange per call
            test_exchange = get_authenticated_exchange(k, load_markets=False)
            test_exchange.fetch_balance()
            is_active = True
            latency_ms = int((time.monotonic() - started) * 1000)
        except Exception as e:
            is_active = False
            error_msg = _humanize_exchange_error(e)

        linked = [
            {"name": b_name, "is_active": bool(b_active), "live": bool((b_settings or {}).get("api_execution"))}
            for b_name, b_active, b_settings in bots
            if (b_settings or {}).get("api_key_name") == k.name
        ]

        result.append({
            "name": k.name,
            "exchange": k.exchange,
            "is_sandbox": k.is_sandbox,
            "market_type": getattr(k, "market_type", None) or DEFAULT_MARKET_TYPE,
            "is_active": is_active,
            "error_msg": error_msg,
            "latency_ms": latency_ms,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "bots": linked,
        })
    return result

@router.get("/{key_name}/balance")
def get_key_balance(key_name: str, db: Session = Depends(get_db)):
    key_record = db.query(ExchangeKey).filter(ExchangeKey.name == key_name).first()
    if not key_record:
        raise HTTPException(status_code=404, detail=f"Key '{key_name}' not found.")

    try:
        exchange = get_authenticated_exchange(key_record, load_markets=False)
        balance_data = exchange.fetch_balance()

        active_balances = {}
        if 'total' in balance_data:
            for coin, amount in balance_data['total'].items():
                if amount and amount > 0:
                    active_balances[coin] = {
                        "free": balance_data['free'].get(coin, 0) or 0,
                        "used": balance_data['used'].get(coin, 0) or 0,
                        "total": amount,
                        "usd_value": None,
                    }

        # Best-effort USD valuation so the wallet shows one total. Stables
        # count as 1; everything else is priced via a direct USD-quoted spot
        # market. A swap key's futures class lists no spot pairs, so the
        # prices come from a public spot instance of the same exchange.
        total_usd = 0.0
        unpriced = []
        try:
            stables = {"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "BUSD", "PYUSD"}
            pricer = exchange
            if key_market_type(key_record) != "spot":
                pricer = build_exchange(key_record.exchange, sandbox=False, market_type="spot")
            pricer.load_markets()
            wanted = {}
            for coin, data in active_balances.items():
                if coin in stables:
                    data["usd_value"] = float(data["total"])
                    continue
                for quote in ("USDT", "USD", "USDC"):
                    sym = f"{coin}/{quote}"
                    if sym in pricer.markets:
                        wanted[sym] = coin
                        break
                else:
                    unpriced.append(coin)
            if wanted:
                try:
                    tickers = pricer.fetch_tickers(list(wanted.keys()))
                except Exception:
                    tickers = {}
                    for sym in wanted:
                        try:
                            tickers[sym] = pricer.fetch_ticker(sym)
                        except Exception:
                            pass
                for sym, coin in wanted.items():
                    last = (tickers.get(sym) or {}).get("last")
                    if last:
                        active_balances[coin]["usd_value"] = float(active_balances[coin]["total"]) * float(last)
                    else:
                        unpriced.append(coin)
            total_usd = sum(d["usd_value"] for d in active_balances.values() if d["usd_value"] is not None)
        except Exception as exc:
            logger.debug("USD valuation skipped for '%s': %s", key_name, type(exc).__name__)

        return {"name": key_name, "balances": active_balances, "total_usd": round(total_usd, 2),
                "valuation_currency": "USD", "unpriced": sorted(set(unpriced))}
    except Exception as e:
        logger.warning("Failed to fetch balance for '%s': %s", key_name, type(e).__name__)
        raise HTTPException(status_code=400, detail="Failed to fetch balance from exchange.")

@router.delete("/{key_name}")
def delete_exchange_keys(key_name: str, db: Session = Depends(get_db)):
    key_record = db.query(ExchangeKey).filter(ExchangeKey.name == key_name).first()
    if not key_record:
        raise HTTPException(status_code=404, detail=f"Key '{key_name}' not found.")

    # A bot that references this key would silently lose the ability to send
    # exits for its real positions (the engine downgrades a keyless live bot to
    # forward test). Refuse while any bot still points at it.
    linked = [
        {"name": b_name, "is_active": bool(b_active), "live": bool((b_settings or {}).get("api_execution"))}
        for b_name, b_active, b_settings in db.query(BotConfig.name, BotConfig.is_active, BotConfig.settings).all()
        if (b_settings or {}).get("api_key_name") == key_name
    ]
    if linked:
        names = ", ".join(f"'{b['name']}'" + (" (running)" if b["is_active"] else "") for b in linked)
        raise HTTPException(status_code=409, detail={
            "message": f"Key '{key_name}' is used by {len(linked)} bot(s): {names}. Switch those bots to another key or delete them first.",
            "bots": linked,
        })

    db.delete(key_record)
    db.commit()
    invalidate_authenticated_exchange(key_name)
    invalidate_leverage_cache(key_name)
    return {"message": f"Key '{key_name}' deleted successfully."}

SWAP_MAX_NOTIONAL = float(os.environ.get("SWAP_MAX_NOTIONAL", "5000"))  # in the market's quote currency
_SWAP_TOKEN_TTL = 600.0
_swap_results: dict = {}  # idempotency_key -> (monotonic ts, response payload)
_swap_lock = threading.Lock()


class SwapRequest(BaseModel):
    from_asset: str = Field(pattern=r"^[A-Z0-9]{2,10}$")
    to_asset: str = Field(pattern=r"^[A-Z0-9]{2,10}$")
    amount: float = Field(gt=0)
    amount_type: Literal["from", "to"] = "from"
    # Client-generated per attempt; a retry with the same token returns the
    # first result instead of placing a second market order
    idempotency_key: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _remember_swap(token: Optional[str], payload):
    if not token:
        return payload
    with _swap_lock:
        now = time.monotonic()
        for k in [k for k, (ts, _) in _swap_results.items() if now - ts > _SWAP_TOKEN_TTL]:
            _swap_results.pop(k, None)
        _swap_results[token] = (now, payload)
    return payload


@router.post("/{name}/swap")
def execute_quick_swap(name: str, payload: SwapRequest, db: Session = Depends(get_db)):
    # Sync endpoint: FastAPI runs it in the threadpool, so the blocking
    # ccxt calls and sleep below don't stall the event loop.
    if payload.idempotency_key:
        with _swap_lock:
            hit = _swap_results.get(payload.idempotency_key)
        if hit:
            return hit[1]
    try:
        key_record = db.query(ExchangeKey).filter(ExchangeKey.name == name).first()
        if not key_record:
            return JSONResponse(status_code=404, content={"detail": f"API Wallet '{name}' not found"})

        exchange = get_authenticated_exchange(key_record)
        ex_id = str(key_record.exchange or "").lower()
        ex_name = SUPPORTED_EXCHANGES.get(ex_id, ex_id)

        from_asset = payload.from_asset
        to_asset = payload.to_asset
        amount = payload.amount
        if from_asset == to_asset:
            return JSONResponse(status_code=400, content={"detail": "From and to asset must differ."})

        exchange.load_markets()
        symbol_buy = f"{to_asset}/{from_asset}"
        symbol_sell = f"{from_asset}/{to_asset}"

        if symbol_buy in exchange.markets:
            symbol, side = symbol_buy, "buy"
            ticker = exchange.fetch_ticker(symbol_buy)
            raw_amount = (amount / ticker['last']) if payload.amount_type == 'from' else amount
        elif symbol_sell in exchange.markets:
            symbol, side = symbol_sell, "sell"
            ticker = exchange.fetch_ticker(symbol_sell)
            raw_amount = amount if payload.amount_type == 'from' else (amount / ticker['last'])
        else:
            return JSONResponse(status_code=400, content={"detail": f"Trading pair {from_asset}/{to_asset} not supported on this environment."})

        last = float(ticker.get('last') or 0)
        if last <= 0:
            return JSONResponse(status_code=400, content={"detail": f"No last price available for {symbol}; refusing to place a market order blind."})
        trade_amount = float(exchange.amount_to_precision(symbol, raw_amount))
        if trade_amount <= 0:
            return JSONResponse(status_code=400, content={"detail": "Amount rounds to zero at exchange precision."})
        notional = trade_amount * last
        quote = symbol.split('/')[1]
        if notional > SWAP_MAX_NOTIONAL:
            return JSONResponse(status_code=400, content={
                "detail": f"Swap of ~{notional:,.2f} {quote} exceeds the safety cap of {SWAP_MAX_NOTIONAL:,.0f} {quote} per swap "
                          f"(SWAP_MAX_NOTIONAL). Split it into smaller swaps."})

        if side == "buy":
            order = exchange.create_market_buy_order(symbol, trade_amount)
        else:
            order = exchange.create_market_sell_order(symbol, trade_amount)

        # Wait briefly then re-fetch to detect orders stuck due to zero liquidity on testnet
        time.sleep(1.0)
        fetched_order = exchange.fetch_order(order['id'], order['symbol'])

        if fetched_order['status'] == 'canceled':
            return JSONResponse(status_code=400, content={"detail": f"{ex_name} canceled the order. Reason: Zero liquidity for {order['symbol']} on the testnet."})
        if fetched_order['status'] == 'open':
            exchange.cancel_order(order['id'], order['symbol'])
            return JSONResponse(status_code=400, content={"detail": f"Order stuck. No volume for {order['symbol']} on the Sandbox. Order auto-canceled to prevent stuck balance."})

        return _remember_swap(payload.idempotency_key, {"status": "success", "order": fetched_order})

    except ccxt.ExchangeError as e:
        error_msg = str(e)
        if ex_id == "okx" and ("51155" in error_msg or "compliance" in error_msg.lower()):
            return JSONResponse(status_code=400, content={"detail": "European Compliance Error (MiCA): You cannot trade USDT on OKX in Europe. Please swap to USDC or EUR instead."})
        return JSONResponse(status_code=400, content={"detail": "Exchange rejected the order. Please check your assets and try again."})
    except ccxt.InsufficientFunds:
        return JSONResponse(status_code=400, content={"detail": "Insufficient funds in your account to cover this swap amount."})
    except ccxt.InvalidOrder:
        return JSONResponse(status_code=400, content={"detail": "Order size too small or invalid for this exchange."})
    except Exception as e:
        logger.error("Swap error for wallet '%s': %s", name, e, exc_info=True)
        return JSONResponse(status_code=400, content={"detail": "An unexpected error occurred during the swap."})
