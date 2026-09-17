import logging
import os
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import asyncio
from contextlib import asynccontextmanager

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

from backend.core.database import engine, Base, run_migrations, run_startup_sweeps
from backend.core.security import verify_api_key

# Ensure SQLAlchemy knows about all models before calling create_all()
from backend.models.positions import Position
from backend.models.orders import Order
from backend.models.candles import Candle
from backend.models.signals import Signal
from backend.models.preferences import Preference
from backend.models.exchange_keys import ExchangeKey
from backend.models.bots import BotConfig
from backend.models.bot_logs import BotLog
from backend.models.bot_config_runs import BotConfigRun

# Import the routers
from backend.routers import auth, keys, data, bots, trades, indicators
# Import the background services
from backend.engine.candle_poller import candle_poller
from backend.engine.bot_manager import bot_manager
from backend.core.exchange_registry import build_exchange

# Create database tables and run migrations for existing DBs
Base.metadata.create_all(bind=engine)
run_migrations()
run_startup_sweeps()

# Lifespan context manager for background tasks
@asynccontextmanager
async def lifespan(app: FastAPI):
    poll_task = asyncio.create_task(candle_poller.start())
    bot_task = asyncio.create_task(bot_manager.start())
    yield
    candle_poller.stop()
    bot_manager.stop()
    # Startup threads (backfill/backtest) and candle handlers are fire-and-
    # forget tasks; cancel them and give everything 10 s to unwind so a
    # reload never hangs on a long backtest or a stuck exchange call
    pending = [poll_task, bot_task, *bot_manager._bg_tasks, *candle_poller._poll_tasks]
    for task in bot_manager._bg_tasks | set(candle_poller._poll_tasks):
        task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10)
    except asyncio.TimeoutError:
        logging.getLogger("apexalgo.main").warning(
            "Shutdown: %d background task(s) still running after 10 s — exiting anyway", sum(1 for t in pending if not t.done()))

# Initialize FastAPI application with the lifespan manager
enable_docs = os.getenv("ENABLE_DOCS", "0") == "1"
app = FastAPI(
    title="ApexAlgo Engine API",
    version="2.1.0",
    swagger_ui_init_oauth={"clientId": "test"},
    lifespan=lifespan,
    docs_url="/docs" if enable_docs else None,
    redoc_url="/redoc" if enable_docs else None,
    openapi_url="/openapi.json" if enable_docs else None,
)

DEFAULT_CORS_ORIGINS = "https://localhost:5173,https://127.0.0.1:5173"
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,  # session cookie; origins are explicit, never "*"
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
# Connect routers to the main application
app.include_router(auth.router)
app.include_router(keys.router)
app.include_router(data.router)
app.include_router(bots.router)
app.include_router(trades.router)
app.include_router(indicators.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/", dependencies=[Depends(verify_api_key)])
def read_root():
    return {"status": "online", "message": "ApexAlgo Engine is running and modularized!"}

@app.get("/api/price/{symbol}", dependencies=[Depends(verify_api_key)])
def get_price(symbol: str, exchange: str = Query(default="okx")):
    try:
        exch = build_exchange(exchange.lower())
        formatted_symbol = symbol.replace('-', '/').upper()
        ticker = exch.fetch_ticker(formatted_symbol)
        return {
            "exchange": exchange.upper(),
            "symbol": formatted_symbol,
            "price": ticker['last'],
            "timestamp": ticker['datetime']
        }
    except Exception as e:
        logging.getLogger("apexalgo.main").warning("Failed to fetch price for '%s' on '%s': %s", symbol, exchange, e)
        raise HTTPException(status_code=400, detail="Failed to fetch price from the exchange.")
