"""Sprint D: contract economics (`engine/contracts.py`), per-currency pools,
the registry's contract kinds, the position-currency migration and the
currency-aware API payloads.

Money never travels as a bare float: every figure a `ContractSpec` returns is
in the spec's cash currency (quote on spot, settle on perpetuals), and a
coin-margined contract keeps its books in the coin.
"""
import math
from datetime import datetime

import pytest
from hypothesis import assume, given, settings, strategies as st
from sqlalchemy import text

from backend.core import exchange_registry as reg
from backend.engine import pnl
from backend.engine.capital import CapitalPools
from backend.engine.contracts import (
    MAINTENANCE_MARGIN, spec_for_instance, spec_from_market, spec_from_symbol,
)
from backend.models.bots import BotConfig
from backend.models.orders import Order
from backend.models.positions import Position
from tests.test_bots_router import HEADERS


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app)

REL = 1e-9
prices = st.floats(min_value=0.01, max_value=1e5, allow_nan=False, allow_infinity=False)
qtys = st.floats(min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False)
leverages = st.floats(min_value=1.0, max_value=125.0, allow_nan=False, allow_infinity=False)
sizes = st.sampled_from([1.0, 10.0, 100.0, 0.001])
sides = st.sampled_from(["long", "short"])

SPOT = spec_from_symbol("BTC/USDT")
EUR = spec_from_symbol("BTC/EUR")
LINEAR = spec_from_symbol("BTC/USDT:USDT")
INVERSE = spec_from_symbol("BTC/USD:BTC", 100.0)


# ── shape ──────────────────────────────────────────────────────────────────

def test_spec_from_symbol_classifies_kind_and_cash_currency():
    assert (SPOT.kind, SPOT.cash_currency, SPOT.market_type) == ("spot", "USDT", "spot")
    assert (EUR.kind, EUR.cash_currency, EUR.quote) == ("spot", "EUR", "EUR")
    assert (LINEAR.kind, LINEAR.cash_currency, LINEAR.settle, LINEAR.market_type) == ("linear", "USDT", "USDT", "swap")
    assert (INVERSE.kind, INVERSE.cash_currency, INVERSE.contract_size, INVERSE.is_inverse) == ("inverse", "BTC", 100.0, True)
    assert spec_from_symbol("ETH/BTC").cash_currency == "BTC"  # BTC-quoted spot pays in BTC
    assert spec_from_symbol("btc-usdt").symbol == "BTC/USDT"


def test_spec_from_market_prefers_the_exchange_metadata():
    m = {"symbol": "BTC/USD:BTC", "base": "BTC", "quote": "USD", "settle": "BTC", "swap": True, "inverse": True,
         "linear": False, "spot": False, "contractSize": 100}
    s = spec_from_market(m)
    assert (s.kind, s.contract_size, s.cash_currency) == ("inverse", 100.0, "BTC")
    lin = spec_from_market({"symbol": "BTC/USDT:USDT", "base": "BTC", "quote": "USDT", "settle": "USDT", "swap": True,
                            "linear": True, "contractSize": 0.001})
    assert (lin.kind, lin.contract_size) == ("linear", 0.001)
    # a market without the flags falls back to the symbol's shape
    assert spec_from_market({"symbol": "ETH/EUR"}).kind == "spot"
    assert spec_from_market(None, "BTC/USD:BTC").kind == "inverse"


def test_spec_for_instance_reads_the_loaded_market():
    class Ex:
        markets = {"BTC/USD:BTC": {"symbol": "BTC/USD:BTC", "base": "BTC", "quote": "USD", "settle": "BTC",
                                   "swap": True, "inverse": True, "contractSize": 10}}
    assert spec_for_instance(Ex(), "BTC/USD:BTC").contract_size == 10.0
    assert spec_for_instance(Ex(), "ETH/USDT").kind == "spot"  # unknown market → symbol


# ── linear / spot arithmetic is the pre-existing engine arithmetic ─────────

@given(qtys, prices, leverages, st.floats(min_value=0, max_value=0.01))
def test_linear_and_spot_keep_the_old_formulas(qty, price, lev, fee):
    n = price * qty
    assert SPOT.locked_capital(qty, price, 1, fee) == n * (1 + fee)
    assert LINEAR.locked_capital(qty, price, lev, fee) == n / lev + n * fee
    assert SPOT.notional_cash(qty, price) == n == LINEAR.notional_quote(qty, price)
    assert LINEAR.pnl_cash("long", qty, price, price * 1.1) == pytest.approx((price * 1.1 - price) * qty, rel=REL)
    assert SPOT.close_return("long", qty, price, price * 0.9, 1, fee) == price * 0.9 * qty * (1 - fee)


@given(sides, prices, leverages)
def test_linear_liquidation_matches_pnl_module(side, entry, lev):
    assume(lev > 1 or side == "short")
    assert LINEAR.liquidation_price(side, entry, lev) == pnl.liquidation_price(side, entry, lev)
    assert pnl.liquidation_price(side, entry, lev, spec=LINEAR) == LINEAR.liquidation_price(side, entry, lev)


# ── inverse arithmetic ─────────────────────────────────────────────────────

def test_inverse_notional_margin_and_fee_are_in_the_coin():
    # 2.5 contracts of 100 USD at 100 USD/BTC = 250 USD = 2.5 BTC notional
    assert INVERSE.notional_quote(2.5, 100.0) == 250.0
    assert INVERSE.notional_cash(2.5, 100.0) == pytest.approx(2.5)
    assert INVERSE.margin(2.5, 100.0, 5) == pytest.approx(0.5)
    assert INVERSE.fee_cash(2.5, 100.0, 0.001) == pytest.approx(0.0025)
    assert INVERSE.qty_for_cash(0.5, 100.0, 5) == pytest.approx(2.5)
    assert INVERSE.qty_for_quote_notional(250.0, 100.0) == 2.5
    assert INVERSE.base_amount(2.5, 100.0) == pytest.approx(2.5)
    # contracts are the position unit: no conversion on the way to ccxt
    assert INVERSE.to_contracts(2.5) == 2.5 and INVERSE.from_contracts(2.5) == 2.5
    assert LINEAR.to_contracts(1.0) == 1.0
    assert spec_from_symbol("BTC/USDT:USDT", 0.001).to_contracts(1.0) == pytest.approx(1000.0)


def test_inverse_pnl_is_convex_in_price():
    # long 1 contract (100 USD) from 100: +10% price → 100/100 − 100/110 BTC
    assert INVERSE.pnl_cash("long", 1.0, 100.0, 110.0) == pytest.approx(100 * (1 / 100 - 1 / 110))
    assert INVERSE.pnl_cash("short", 1.0, 100.0, 110.0) == pytest.approx(-100 * (1 / 100 - 1 / 110))
    # a long loses more coin on the way down than it gains on the way up
    assert -INVERSE.pnl_cash("long", 1.0, 100.0, 90.0) > INVERSE.pnl_cash("long", 1.0, 100.0, 110.0)
    assert INVERSE.pnl_cash("long", 1.0, 0.0, 110.0) == 0.0  # no price, no PnL


@given(qtys, prices, prices, sizes)
def test_inverse_pnl_is_antisymmetric_in_side(qty, entry, exit_, size):
    spec = spec_from_symbol("BTC/USD:BTC", size)
    long_ = spec.pnl_cash("long", qty, entry, exit_)
    short = spec.pnl_cash("short", qty, entry, exit_)
    assert long_ == pytest.approx(-short, rel=REL, abs=1e-12)
    if exit_ != entry:
        assert math.copysign(1, long_) == math.copysign(1, exit_ - entry)


@given(qtys, prices, prices, sizes)
def test_inverse_pnl_equals_the_quote_pnl_valued_at_exit(qty, entry, exit_, size):
    # dir·contracts·size·(1/entry − 1/exit) == quote PnL (contracts·size·(exit/entry − 1)) / exit
    spec = spec_from_symbol("BTC/USD:BTC", size)
    quote_pnl = qty * size * (exit_ / entry - 1)
    assert spec.pnl_cash("long", qty, entry, exit_) == pytest.approx(quote_pnl / exit_, rel=1e-7, abs=1e-12)


@given(sides, prices, st.floats(min_value=1.0, max_value=125.0))
def test_inverse_liquidation_lies_on_the_losing_side_and_closer_with_leverage(side, entry, lev):
    liq = INVERSE.liquidation_price(side, entry, lev)
    assert liq is not None and liq > 0
    if side == "long":
        assert liq < entry
        assert liq == pytest.approx(entry * lev / (lev + 1 - MAINTENANCE_MARGIN), rel=REL)
        # at the liquidation price the long has lost (almost) its whole margin
        loss = -INVERSE.pnl_cash("long", 1.0, entry, liq)
        assert loss == pytest.approx(INVERSE.margin(1.0, entry, lev) * (1 - MAINTENANCE_MARGIN), rel=1e-6)
    else:
        assert liq > entry
        assert liq == pytest.approx(entry * lev / (lev - 1 + MAINTENANCE_MARGIN), rel=REL)
        loss = -INVERSE.pnl_cash("short", 1.0, entry, liq)
        assert loss == pytest.approx(INVERSE.margin(1.0, entry, lev) * (1 - MAINTENANCE_MARGIN), rel=1e-6)
    if lev < 100:
        further = INVERSE.liquidation_price(side, entry, lev * 1.25)
        assert abs(further - entry) < abs(liq - entry)


def test_inverse_liquidation_at_1x():
    # a 1x inverse short can never be liquidated by the formula's denominator
    # going to zero: lev − 1 + mmr = mmr → far above entry, still finite
    assert INVERSE.liquidation_price("short", 100.0, 1) == pytest.approx(100 / MAINTENANCE_MARGIN)
    assert INVERSE.liquidation_price("long", 100.0, 1) == pytest.approx(100 / (2 - MAINTENANCE_MARGIN))
    assert LINEAR.liquidation_price("long", 100.0, 1) is None
    assert INVERSE.liquidation_price("long", 0, 5) is None


@given(sides, qtys, prices, prices, leverages, st.floats(min_value=0, max_value=0.01))
def test_realized_pnl_is_close_return_minus_locked_capital_for_every_kind(side, qty, entry, exit_, lev, fee):
    for spec in (LINEAR, INVERSE):
        got = spec.realized_pnl(side, qty, entry, exit_, lev, fee, fee)
        want = spec.pnl_cash(side, qty, entry, exit_) - spec.fee_cash(qty, entry, fee) - spec.fee_cash(qty, exit_, fee)
        # margin − margin cancels in floating point: tolerance scales with the notional
        tol = 1e-12 * max(spec.notional_cash(qty, entry), spec.notional_cash(qty, exit_), 1.0)
        assert got == pytest.approx(want, rel=1e-7, abs=tol)
    assert SPOT.realized_pnl("long", qty, entry, exit_, 1, fee, fee) == pytest.approx(
        exit_ * qty * (1 - fee) - entry * qty * (1 + fee), rel=1e-7, abs=1e-12 * max(qty * max(entry, exit_), 1.0))


def test_max_affordable_qty_fits_the_cash_exactly():
    for spec, lev in ((SPOT, 1), (LINEAR, 5), (INVERSE, 5)):
        q = spec.max_affordable_qty(0.5, 100.0, lev, 0.001)
        assert spec.locked_capital(q, 100.0, lev, 0.001) == pytest.approx(0.5)


def test_fee_cash_from_fill_converts_into_the_cash_currency():
    assert SPOT.fee_cash_from_fill({"currency": "USDT", "cost": 1.5}, 100.0) == 1.5
    assert SPOT.fee_cash_from_fill({"currency": "BTC", "cost": 0.01}, 100.0) == pytest.approx(1.0)
    assert INVERSE.fee_cash_from_fill({"currency": "BTC", "cost": 0.01}, 100.0) == 0.01
    assert INVERSE.fee_cash_from_fill({"currency": "USD", "cost": 2.0}, 100.0) == pytest.approx(0.02)
    assert SPOT.fee_cash_from_fill({"currency": "BNB", "cost": 0.1}, 100.0) is None  # needs a rate
    assert SPOT.fee_cash_from_fill(None, 100.0) is None


def test_mark_value_per_kind():
    class P:
        def __init__(self, side, amount, entry):
            self.side, self.amount, self.entry_price = side, amount, entry
    assert SPOT.mark_value([P("long", 2.0, 100.0)], 110.0) == 220.0
    assert LINEAR.mark_value([P("long", 2.0, 100.0)], 110.0, 5) == pytest.approx(200 / 5 + 20)
    assert INVERSE.mark_value([P("short", 1.0, 100.0)], 110.0, 5) == pytest.approx(
        INVERSE.margin(1.0, 100.0, 5) + INVERSE.pnl_cash("short", 1.0, 100.0, 110.0))


# ── pnl.py wrappers ────────────────────────────────────────────────────────

def test_pnl_module_dispatches_to_the_spec():
    assert pnl.price_pnl("long", 100.0, 110.0, 1.0, spec=INVERSE) == INVERSE.pnl_cash("long", 1.0, 100.0, 110.0)
    assert pnl.price_pnl("long", 100.0, 110.0, 1.0) == 10.0
    assert pnl.locked_capital(100.0, 1.0, 5, spec=INVERSE) == INVERSE.locked_capital(1.0, 100.0, 5)
    assert pnl.locked_capital(100.0, 1.0, 5) == 20.0
    assert pnl.locked_capital(100.0, 1.0, 5, spec=LINEAR) == 20.0


# ── capital pools ──────────────────────────────────────────────────────────

def test_capital_pools_keep_currencies_apart_and_never_go_negative():
    pools = CapitalPools.for_bot("BTC", 1.0)
    assert pools.single_currency() == "BTC" and pools.cash("BTC") == 1.0 and pools.start("BTC") == 1.0
    pools.lock("BTC", 0.4)
    assert pools.cash("BTC") == pytest.approx(0.6) and pools.pool("BTC").locked == pytest.approx(0.4)
    pools.release("BTC", 0.45, locked=0.4)
    assert pools.cash("BTC") == pytest.approx(1.05) and pools.pool("BTC").locked == 0.0
    pools.lock("BTC", 5.0)  # no spot margin: floors at zero instead of going negative
    assert pools.cash("BTC") == 0.0
    pools.forget("BTC", 5.0)
    assert pools.pool("BTC").locked == 0.0
    assert pools.cash("USDT") == 0.0 and pools.currencies() == ["BTC", "USDT"]
    assert pools.single_currency() is None


# ── registry kinds ─────────────────────────────────────────────────────────

def test_registry_kinds_binance_inverse_is_its_own_ccxt_class():
    assert reg.supported_kinds("binance") == ("linear", "inverse")
    assert reg.ccxt_id_for("binance", "swap") == "binance"
    assert reg.ccxt_id_for("binance", "swap", "inverse") == "binancecoinm"
    assert reg.ccxt_id_for("okx", "swap", "inverse") == "okx"
    assert reg.supported_kinds("bingx") == ("linear",)
    assert reg.supported_kinds("bitvavo") == ()  # no swap market at all
    assert reg.kind_of_symbol("BTC/USD:BTC") == "inverse" and reg.kind_of_symbol("BTC/USDT:USDT") == "linear"
    assert reg.kind_of_symbol("BTC/EUR") == "spot"
    with pytest.raises(ValueError):
        reg.build_exchange("bingx", market_type="swap", kind="inverse")
    ex = reg.build_exchange("binance", market_type="swap", kind="inverse")
    assert ex.id == "binancecoinm"


def test_registry_contract_spec_cache_never_hits_the_network(monkeypatch):
    monkeypatch.setattr(reg, "_spec_cache", {})
    # nothing cached: the symbol's own shape, default contract size
    assert reg.contract_spec("binance", "BTC/USD:BTC").contract_size == 1.0
    reg.remember_markets("binance", {"BTC/USD:BTC": {"symbol": "BTC/USD:BTC", "base": "BTC", "quote": "USD", "settle": "BTC",
                                                      "swap": True, "inverse": True, "contractSize": 100}})
    assert reg.contract_spec("binance", "BTC/USD:BTC").contract_size == 100.0
    assert reg.contract_spec("okx", "BTC/USD:BTC").contract_size == 1.0  # per exchange


class _Key:
    def __init__(self, exchange, market_type):
        self.exchange, self.market_type, self.name, self.is_sandbox = exchange, market_type, "k", False


def test_key_kind_for_symbol_and_auth_cache_key():
    assert reg.key_kind_for_symbol(_Key("binance", "spot"), "BTC/USDT") is None
    assert reg.key_kind_for_symbol(_Key("binance", "swap"), "BTC/USD:BTC") == "inverse"
    assert reg.key_kind_for_symbol(_Key("binance", "swap"), "BTC/USDT:USDT") == "linear"
    assert reg.key_kind_for_symbol(_Key("binance", "swap"), None) == "linear"
    # kinds served by one ccxt class share an instance; binancecoinm gets its own
    assert reg._auth_key(_Key("okx", "swap"), "inverse") == reg._auth_key(_Key("okx", "swap"), "linear")
    assert reg._auth_key(_Key("binance", "swap"), "inverse") != reg._auth_key(_Key("binance", "swap"), "linear")


def test_bot_manager_routes_binance_inverse_to_its_own_instance():
    from backend.engine.bot_manager import BotManager
    bm = BotManager()
    assert bm._needs_own_instance(_Key("binance", "swap"), "BTC/USD:BTC") is True
    assert bm._needs_own_instance(_Key("binance", "swap"), "BTC/USDT:USDT") is False
    assert bm._needs_own_instance(_Key("okx", "swap"), "BTC/USD:BTC") is False
    assert bm._needs_own_instance(_Key("binance", "spot"), "BTC/USD:BTC") is False


# ── migration backfill ─────────────────────────────────────────────────────

def test_migration_backfills_position_currency_from_the_symbol(db):
    from backend.core.database import engine, run_migrations
    db.add(BotConfig(name="mig", is_active=False, is_sandbox=True, strategy="node_graph", settings={}))
    db.commit()
    for sym in ("BTC/USDT", "ETH/BTC", "BTC/USD:BTC", "ETH/USDT:USDT"):
        db.add(Position(exchange="binance", bot_name="mig", symbol=sym, mode="backtest", status="closed",
                        side="long", entry_price=1.0, amount=1.0, profit_abs=0.0))
    db.commit()
    with engine.begin() as conn:
        conn.execute(text("UPDATE positions SET cash_currency = NULL, contract_kind = NULL"))
        conn.execute(text("UPDATE positions SET cash_currency = 'KEEP' WHERE symbol = 'ETH/USDT:USDT'"))
    run_migrations()
    db.expire_all()
    got = {p.symbol: (p.cash_currency, p.contract_kind) for p in db.query(Position).all()}
    assert got == {"BTC/USDT": ("USDT", "spot"), "ETH/BTC": ("BTC", "spot"),
                   "BTC/USD:BTC": ("BTC", "inverse"), "ETH/USDT:USDT": ("KEEP", "linear")}
    run_migrations()  # idempotent


# ── API payloads ───────────────────────────────────────────────────────────

def _closed(db, bot, symbol, pnl_abs, mode="backtest", ccy=None, kind=None, fee=0.1):
    p = Position(exchange="binance", bot_name=bot, symbol=symbol, mode=mode, status="closed", side="long",
                 entry_price=100.0, amount=1.0, profit_abs=pnl_abs, profit_pct=pnl_abs, cash_currency=ccy, contract_kind=kind,
                 created_at=datetime(2024, 1, 1), closed_at=datetime(2024, 1, 2))
    db.add(p)
    db.flush()
    db.add(Order(position_id=p.id, exchange="binance", bot_name=bot, mode=mode, symbol=symbol, side="buy", order_type="market",
                 price=100.0, amount=1.0, fee=fee, timestamp=datetime(2024, 1, 1), status="filled", fee_currency=ccy))
    return p


def test_stats_are_split_per_cash_currency(db, client):
    for name in ("usdt-bot", "btc-bot"):
        db.add(BotConfig(name=name, is_active=False, is_sandbox=True, strategy="node_graph", settings={"backtest_capital": 1000}))
    db.commit()
    _closed(db, "usdt-bot", "BTC/USDT", 10.0, ccy="USDT", kind="spot")
    _closed(db, "usdt-bot", "BTC/USDT", -4.0, ccy="USDT", kind="spot")
    _closed(db, "btc-bot", "BTC/USD:BTC", 0.02, ccy="BTC", kind="inverse", fee=0.001)
    _closed(db, "btc-bot", "ETH/BTC", 0.01, ccy=None, kind=None, fee=0.001)  # legacy row: currency from the symbol
    db.commit()

    single = client.get("/api/trades/stats?bot_name=usdt-bot", headers=HEADERS).json()
    assert single["cash_currency"] == "USDT" and single["netPnl"] == 6.0 and single["total"] == 2
    assert single["by_currency"]["USDT"]["netPnl"] == 6.0 and single["by_currency"]["USDT"]["totalFees"] == pytest.approx(0.2)
    assert set(single["by_currency"]) == {"USDT"}

    mixed = client.get("/api/trades/stats", headers=HEADERS).json()
    assert set(mixed["by_currency"]) == {"BTC", "USDT"}
    assert mixed["by_currency"]["BTC"]["netPnl"] == pytest.approx(0.03) and mixed["by_currency"]["BTC"]["total"] == 2
    assert mixed["by_currency"]["USDT"]["netPnl"] == 6.0
    # never a BTC + USDT sum: money fields are None across currencies, counts add up
    assert mixed["cash_currency"] is None and mixed["netPnl"] is None and mixed["totalFees"] is None
    assert mixed["total"] == 4 and mixed["wins"] == 3 and mixed["losses"] == 1
    assert mixed["bySide"]["long"]["total"] == 4

    empty = client.get("/api/trades/stats?bot_name=nobody", headers=HEADERS).json()
    assert empty["by_currency"] == {} and empty["total"] == 0


def test_positions_and_orders_carry_the_money_unit(db, client):
    db.add(BotConfig(name="btc-bot", is_active=False, is_sandbox=True, strategy="node_graph", settings={}))
    db.commit()
    _closed(db, "btc-bot", "BTC/USD:BTC", 0.02, ccy="BTC", kind="inverse", fee=0.001)
    legacy = _closed(db, "btc-bot", "BTC/EUR", 3.0)
    legacy.contract_size = None
    db.commit()
    rows = {r["symbol"]: r for r in client.get("/api/trades/positions", headers=HEADERS).json()}
    assert (rows["BTC/USD:BTC"]["cash_currency"], rows["BTC/USD:BTC"]["contract_kind"]) == ("BTC", "inverse")
    assert (rows["BTC/EUR"]["cash_currency"], rows["BTC/EUR"]["contract_kind"], rows["BTC/EUR"]["contract_size"]) == ("EUR", "spot", 1.0)
    orders = {o["symbol"]: o for o in client.get("/api/trades/orders", headers=HEADERS).json()}
    assert orders["BTC/USD:BTC"]["cash_currency"] == "BTC" and orders["BTC/USD:BTC"]["fee_currency"] == "BTC"
    assert orders["BTC/EUR"]["cash_currency"] == "EUR" and orders["BTC/EUR"]["fee_currency"] == "EUR"
    assert orders["BTC/EUR"]["fee_cash"] is None and orders["BTC/EUR"]["contract_kind"] == "spot"


def test_bot_summary_names_the_cash_currency(db, client):
    db.add(BotConfig(name="eur", is_active=False, is_sandbox=True, strategy="node_graph",
                     settings={"symbols": ["BTC/EUR"], "data_exchange": "bitvavo", "timeframe": "1h"}))
    db.add(BotConfig(name="inv", is_active=False, is_sandbox=True, strategy="node_graph",
                     settings={"symbols": ["BTC/USD:BTC"], "market_type": "swap", "timeframe": "1h",
                               "last_backtest_summary": {"cash_currency": "BTC"}}))
    db.add(BotConfig(name="legacy", is_active=False, is_sandbox=True, strategy="node_graph",
                     settings={"symbol": "eth-usdt", "timeframe": "1h"}))
    db.add(BotConfig(name="blank", is_active=False, is_sandbox=True, strategy="node_graph", settings={}))
    db.commit()
    got = {b["name"]: b["settings"]["cash_currency"] for b in client.get("/api/bots/summary", headers=HEADERS).json()}
    assert got == {"eur": "EUR", "inv": "BTC", "legacy": "USDT", "blank": None}


def test_eur_spot_backtest_books_in_eur(db):
    """A BTC/EUR bot on bitvavo: the pool, fees and summary are EUR."""
    from backend.engine.bot_manager import BotManager
    from tests.conftest import insert_candles, load_example, make_candles
    _, settings = load_example("Donchian_Breakout_1d")
    settings = {**settings, "symbols": ["BTC/EUR"], "symbol": None, "data_exchange": "bitvavo", "backtest_lookback": 600,
                "backtest_on_start": True, "api_execution": False, "api_key_name": None}
    insert_candles(db, make_candles("bitvavo", "BTC/EUR", settings["timeframe"], 700, seed=11, start_price=100.0))
    bot = BotConfig(name="eur-bot", is_active=True, is_sandbox=True, strategy="node_graph", settings=settings)
    db.add(bot)
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    summary = db.get(BotConfig, bot.id).settings["last_backtest_summary"]
    assert summary["cash_currency"] == "EUR" and summary["trades"] > 0
    pos = db.query(Position).filter(Position.bot_name == "eur-bot").all()
    assert pos and all((p.cash_currency, p.contract_kind) == ("EUR", "spot") for p in pos)
    assert {o.fee_currency for o in db.query(Order).filter(Order.bot_name == "eur-bot").all()} == {"EUR"}


@settings(max_examples=30)
@given(prices, st.floats(min_value=0.001, max_value=10.0), leverages)
def test_inverse_backtest_sizing_locks_exactly_the_margin(price, cash, lev):
    qty = INVERSE.qty_for_cash(cash, price, lev)
    assert INVERSE.margin(qty, price, lev) == pytest.approx(cash, rel=1e-9)
    assert INVERSE.notional_quote(qty, price) == pytest.approx(cash * price * lev, rel=1e-9)
