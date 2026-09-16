import logging
import time
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import ccxt

from backend.core.database import get_db
from backend.models.exchange_keys import ExchangeKey
from backend.models.bots import BotConfig
from backend.core.security import verify_api_key
from backend.core.encryption import encrypt_data
from backend.core.exchange_registry import build_exchange, build_exchange_from_key, SUPPORTED_EXCHANGES

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


# Where to create API keys + what the user must switch off. Shown in the UI
# next to the form so nobody has to leave the app to figure this out.
_EXCHANGE_GUIDE = {
    "okx":       {"keys_url": "https://www.okx.com/account/my-api",            "sandbox_note": "Demo trading keys are created under Trade → Demo trading → API."},
    "binance":   {"keys_url": "https://www.binance.com/en/my/settings/api-management", "sandbox_note": "Spot testnet keys come from testnet.binance.vision (separate account)."},
    "bitvavo":   {"keys_url": "https://account.bitvavo.com/user/api",           "sandbox_note": None},
    "coinbase":  {"keys_url": "https://www.coinbase.com/settings/api",          "sandbox_note": None},
    "cryptocom": {"keys_url": "https://crypto.com/exchange/user/settings/api-management", "sandbox_note": "UAT sandbox keys are issued via the Crypto.com Exchange UAT environment."},
    "kraken":    {"keys_url": "https://www.kraken.com/u/security/api",          "sandbox_note": None},
    "kucoin":    {"keys_url": "https://www.kucoin.com/account/api",             "sandbox_note": None},
}


@router.get("/exchanges")
def list_exchanges():
    """Static capabilities per supported exchange for the connection form."""
    out = []
    for ex_id, name in SUPPORTED_EXCHANGES.items():
        has_sandbox = False
        try:
            has_sandbox = bool(getattr(ccxt, ex_id)().urls.get("test"))
        except Exception:
            pass
        guide = _EXCHANGE_GUIDE.get(ex_id, {})
        out.append({
            "id": ex_id,
            "name": name,
            "needs_passphrase": ex_id in ("okx", "kucoin"),
            "has_sandbox": has_sandbox,
            "keys_url": guide.get("keys_url"),
            "sandbox_note": guide.get("sandbox_note"),
        })
    return out


@router.post("")
def save_exchange_keys(req: ExchangeKeyCreate, db: Session = Depends(get_db)):
    exchange_id = req.exchange.lower()
    if exchange_id not in SUPPORTED_EXCHANGES:
        raise HTTPException(status_code=400, detail=f"Unsupported exchange '{exchange_id}'.")
    try:
        test_exchange = build_exchange(
            exchange_id,
            api_key=req.api_key,
            api_secret=req.api_secret,
            passphrase=req.passphrase or None,
            sandbox=req.is_sandbox,
        )
        test_exchange.fetch_balance()
    except Exception as e:
        logger.warning("Exchange key validation failed for '%s': %s", req.name, type(e).__name__)
        raise HTTPException(status_code=400, detail="Connection rejected: Could not authenticate with the provided credentials.")

    try:
        enc_key = encrypt_data(req.api_key)
        enc_secret = encrypt_data(req.api_secret)
        enc_passphrase = encrypt_data(req.passphrase)

        existing = db.query(ExchangeKey).filter(ExchangeKey.name == req.name).first()

        if existing:
            existing.api_key = enc_key
            existing.api_secret = enc_secret
            existing.passphrase = enc_passphrase
            existing.is_sandbox = req.is_sandbox
            existing.exchange = req.exchange
        else:
            new_key = ExchangeKey(
                name=req.name,
                exchange=req.exchange,
                api_key=enc_key,
                api_secret=enc_secret,
                passphrase=enc_passphrase,
                is_sandbox=req.is_sandbox
            )
            db.add(new_key)

        db.commit()
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
            test_exchange = build_exchange_from_key(k)
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
        exchange = build_exchange_from_key(key_record)
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
        # count as 1; everything else is priced via a direct USD-quoted market.
        total_usd = 0.0
        unpriced = []
        try:
            stables = {"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "BUSD", "PYUSD"}
            exchange.load_markets()
            wanted = {}
            for coin, data in active_balances.items():
                if coin in stables:
                    data["usd_value"] = float(data["total"])
                    continue
                for quote in ("USDT", "USD", "USDC"):
                    sym = f"{coin}/{quote}"
                    if sym in exchange.markets:
                        wanted[sym] = coin
                        break
                else:
                    unpriced.append(coin)
            if wanted:
                try:
                    tickers = exchange.fetch_tickers(list(wanted.keys()))
                except Exception:
                    tickers = {}
                    for sym in wanted:
                        try:
                            tickers[sym] = exchange.fetch_ticker(sym)
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

        return {"name": key_name, "balances": active_balances, "total_usd": round(total_usd, 2), "unpriced": sorted(set(unpriced))}
    except Exception as e:
        logger.warning("Failed to fetch balance for '%s': %s", key_name, type(e).__name__)
        raise HTTPException(status_code=400, detail="Failed to fetch balance from exchange.")

@router.delete("/{key_name}")
def delete_exchange_keys(key_name: str, db: Session = Depends(get_db)):
    key_record = db.query(ExchangeKey).filter(ExchangeKey.name == key_name).first()
    if not key_record:
        raise HTTPException(status_code=404, detail=f"Key '{key_name}' not found.")

    db.delete(key_record)
    db.commit()
    return {"message": f"Key '{key_name}' deleted successfully."}

@router.post("/{name}/swap")
def execute_quick_swap(name: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    # Sync endpoint: FastAPI runs it in the threadpool, so the blocking
    # ccxt calls and sleep below don't stall the event loop.
    try:
        key_record = db.query(ExchangeKey).filter(ExchangeKey.name == name).first()
        if not key_record:
            return JSONResponse(status_code=404, content={"detail": f"API Wallet '{name}' not found"})

        exchange = build_exchange_from_key(key_record)

        from_asset = payload.get('from_asset', '').upper()
        to_asset = payload.get('to_asset', '').upper()
        amount = float(payload.get('amount', 0))

        exchange.load_markets()
        symbol_buy = f"{to_asset}/{from_asset}"
        symbol_sell = f"{from_asset}/{to_asset}"

        if symbol_buy in exchange.markets:
            ticker = exchange.fetch_ticker(symbol_buy)
            raw_amount = (amount / ticker['last']) if payload.get('amount_type') == 'from' else amount
            trade_amount = float(exchange.amount_to_precision(symbol_buy, raw_amount))
            order = exchange.create_market_buy_order(symbol_buy, trade_amount)

        elif symbol_sell in exchange.markets:
            ticker = exchange.fetch_ticker(symbol_sell)
            raw_amount = amount if payload.get('amount_type') == 'from' else (amount / ticker['last'])
            trade_amount = float(exchange.amount_to_precision(symbol_sell, raw_amount))
            order = exchange.create_market_sell_order(symbol_sell, trade_amount)
        else:
            return JSONResponse(status_code=400, content={"detail": f"Trading pair {from_asset}/{to_asset} not supported on this environment."})

        # Wait briefly then re-fetch to detect orders stuck due to zero liquidity on testnet
        time.sleep(1.0)
        fetched_order = exchange.fetch_order(order['id'], order['symbol'])

        if fetched_order['status'] == 'canceled':
            return JSONResponse(status_code=400, content={"detail": f"OKX canceled the order. Reason: Zero liquidity for {order['symbol']} on the Testnet."})
        if fetched_order['status'] == 'open':
            exchange.cancel_order(order['id'], order['symbol'])
            return JSONResponse(status_code=400, content={"detail": f"Order stuck. No volume for {order['symbol']} on the Sandbox. Order auto-canceled to prevent stuck balance."})

        return {"status": "success", "order": fetched_order}

    except ccxt.ExchangeError as e:
        error_msg = str(e)
        if "51155" in error_msg or "compliance" in error_msg.lower():
            return JSONResponse(status_code=400, content={"detail": "European Compliance Error (MiCA): You cannot trade USDT on OKX in Europe. Please swap to USDC or EUR instead."})
        return JSONResponse(status_code=400, content={"detail": "Exchange rejected the order. Please check your assets and try again."})
    except ccxt.InsufficientFunds:
        return JSONResponse(status_code=400, content={"detail": "Insufficient funds in your account to cover this swap amount."})
    except ccxt.InvalidOrder:
        return JSONResponse(status_code=400, content={"detail": "Order size too small or invalid for this exchange."})
    except Exception as e:
        logger.error("Swap error for wallet '%s': %s", name, e, exc_info=True)
        return JSONResponse(status_code=400, content={"detail": "An unexpected error occurred during the swap."})
