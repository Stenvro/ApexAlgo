"""Backtest = forward test = live (mocked exchange), trade for trade.

One hand-written candle series per side is pushed through the three
execution paths and every trade is compared on entry price, amount, exit
fills, `profit_abs` and `profit_pct`:

* backtest  — `execute_sync_backfill` over the whole series;
* forward   — the same bot ticked candle by candle (`_process_bots` with an
              explicit `candle_ts`), sized from the carried pool;
* live      — a second bot bound to an exchange key, ticked the same way
              against `ParityExchange`, a mock that fills at the engine's own
              price (no `average`), charges the configured fee rate and whose
              free balance is the wallet the fills imply.

Signals are driven by the volume column (`volume > 900` opens, `volume <
0.5` closes) so the fixture decides exactly which candle trades. Each series
contains a take profit, a stop loss at its trigger, a gap through the stop
that fills at the open, and a strategy exit; a second series liquidates a 5x
position on a candle whose stop fill would lie beyond the liquidation level.

Divergences that exist by definition are asserted, not hidden:

* forward/backtest pay the configured slippage, live fills at the exchange
  price — the live comparison therefore runs with slippage 0 (the fee is
  reported by the mock at the configured rate, in quote on entries and in
  base on closes, exactly as `fee_in_quote` books it);
* a live liquidation is only discovered when a reduce-only close is
  rejected and `fetch_positions` shows nothing, so the close order is booked
  at the candle close, not at the model's liquidation price; the loss
  (margin + entry fee) is identical.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from backend.core import exchange_registry as reg
from backend.engine import broker, pnl
from backend.engine.bot_manager import BotManager
from backend.engine.contracts import spec_from_symbol
from backend.engine.sizing import position_spec
from backend.models.bots import BotConfig
from backend.models.candles import Candle
from backend.models.exchange_keys import ExchangeKey
from backend.models.orders import Order
from backend.models.positions import Position
from tests.conftest import insert_candles
from tests.test_live_tick import EXCHANGE, TF, KEY_NAME
from tests.test_swap import SWAP, CONTRACT_SIZE, SwapExchangeMock

SPOT = "BTC/USDT"
CAPITAL = 1000.0
FEE_PCT = 0.1
WARMUP = 20  # quiet candles so every tick sees the 20-row minimum
START = datetime(2024, 1, 1)

# (open, high, low, close, volume) — volume 1000 opens, 0.1 closes, 1 is quiet
LONG_SERIES = [
    (100, 101, 99, 100, 1000),   # open long @ 100
    (100, 103, 98, 102, 1),      # nothing (SL 90 / TP 108 untouched)
    (103, 110, 102, 105, 1),     # TP 8% -> 108, fill max(108, 103) = 108
    (104, 106, 103, 105, 1000),  # open @ 105
    (100, 101, 92, 95, 1),       # SL 10% -> 94.5, low 92, fill min(94.5, 100) = 94.5
    (95, 96, 94, 95, 1000),      # open @ 95
    (84, 86, 83, 85, 1),         # gap through SL 85.5: fill at the open 84
    (85, 86, 84, 85, 1000),      # open @ 85
    (85, 87, 84, 86, 0.1),       # strategy exit at the close 86
    (86, 87, 85, 86, 1),         # flat
]
SHORT_SERIES = [(200 - o, 200 - lo, 200 - h, 200 - c, v) for (o, h, lo, c, v) in LONG_SERIES]

# 5x, SL 25%: the stop fill lies beyond the liquidation level -> liquidated
LIQ_LONG = [(100, 101, 99, 100, 1000), (95, 96, 60, 70, 1), (70, 71, 69, 70, 1)]
LIQ_SHORT = [(200 - o, 200 - lo, 200 - h, 200 - c, v) for (o, h, lo, c, v) in LIQ_LONG]

# Sprint D: an inverse (coin-margined) contract — BTC/USD settled in BTC,
# 100 USD per contract — and a EUR-quoted spot pair. The money of these bots
# is BTC resp. EUR: the pool, fees, PnL and the balance the mock reports.
INVERSE = "BTC/USD:BTC"
INVERSE_SIZE = 100.0
EUR_SPOT = "BTC/EUR"
INVERSE_CAPITAL = 1.0  # BTC

CASES = {
    "long_spot_1x": dict(symbol=SPOT, market="spot", leverage=1, side="long", series=LONG_SERIES, sl=10),
    "long_swap_5x": dict(symbol=SWAP, market="swap", leverage=5, side="long", series=LONG_SERIES, sl=10),
    "short_swap_5x": dict(symbol=SWAP, market="swap", leverage=5, side="short", series=SHORT_SERIES, sl=10),
    "liq_long_swap_5x": dict(symbol=SWAP, market="swap", leverage=5, side="long", series=LIQ_LONG, sl=25),
    "liq_short_swap_5x": dict(symbol=SWAP, market="swap", leverage=5, side="short", series=LIQ_SHORT, sl=25),
    "long_eur_spot_1x": dict(symbol=EUR_SPOT, market="spot", leverage=1, side="long", series=LONG_SERIES, sl=10, cash="EUR"),
    "long_inverse_5x": dict(symbol=INVERSE, market="swap", leverage=5, side="long", series=LONG_SERIES, sl=10, cash="BTC", capital=INVERSE_CAPITAL),
    "short_inverse_5x": dict(symbol=INVERSE, market="swap", leverage=5, side="short", series=SHORT_SERIES, sl=10, cash="BTC", capital=INVERSE_CAPITAL),
    "liq_long_inverse_5x": dict(symbol=INVERSE, market="swap", leverage=5, side="long", series=LIQ_LONG, sl=25, cash="BTC", capital=INVERSE_CAPITAL),
    "liq_short_inverse_5x": dict(symbol=INVERSE, market="swap", leverage=5, side="short", series=LIQ_SHORT, sl=25, cash="BTC", capital=INVERSE_CAPITAL),
}
INVERSE_MARKET = {"symbol": INVERSE, "type": "swap", "swap": True, "spot": False, "linear": False, "inverse": True,
                  "base": "BTC", "quote": "USD", "settle": "BTC", "contractSize": INVERSE_SIZE,
                  "precision": {"amount": 1}, "limits": {"amount": {"min": 1}, "cost": {"min": 5.0}}}
EUR_MARKET = {"symbol": EUR_SPOT, "type": "spot", "spot": True, "base": "BTC", "quote": "EUR",
              "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5.0}}}


def _spec(case):
    return spec_from_symbol(case["symbol"], INVERSE_SIZE if case["symbol"] == INVERSE else None)


def _capital(case):
    return case.get("capital", CAPITAL)


# ── fixtures ───────────────────────────────────────────────────────────────

def _candles(symbol, series):
    rows = [(100, 100, 100, 100, 1)] * WARMUP + list(series)
    return [Candle(exchange=EXCHANGE, symbol=symbol, timeframe=TF, timestamp=START + timedelta(hours=i),
                   open=float(o), high=float(h), low=float(lo), close=float(c), volume=float(v))
            for i, (o, h, lo, c, v) in enumerate(rows)]


def _settings(case, *, slippage, live_key=None, backtest=True):
    open_node = {"class": "condition", "left": "volume", "operator": ">", "right": 900}
    close_node = {"class": "condition", "left": "volume", "operator": "<", "right": 0.5}
    never = {"class": "condition", "left": "close", "operator": "<", "right": 0}
    short = case["side"] == "short"
    s = {
        "symbols": [case["symbol"]], "timeframe": TF, "data_exchange": EXCHANGE,
        "api_execution": live_key is not None, "api_key_name": live_key,
        "backtest_on_start": backtest, "backtest_lookback": WARMUP + len(case["series"]),
        "backtest_capital": _capital(case), "max_positions": 1, "max_positions_scope": "per_pair",
        "max_order_value": 0, "live_allocation_pct": 100,
        "market_type": case["market"], "leverage": case["leverage"], "margin_mode": "isolated",
        "trade_settings": {
            "entry": {"order_type": "market", "amount_type": "percentage", "amount_value": 50,
                      "fee": FEE_PCT, "slippage": slippage,
                      "stop_losses": [{"type": "percentage", "value": case["sl"], "close_amount_type": "percentage", "close_amount_value": 100}],
                      "take_profits": [{"type": "percentage", "value": 8, "close_amount_type": "percentage", "close_amount_value": 100}]},
            "exit": {"order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": FEE_PCT, "slippage": slippage},
        },
        "nodes": {"open": open_node, "close": close_node, "never": never},
        "entry_node": "never" if short else "open",
        "exit_node": None if short else "close",
        "short_node": "open" if short else None,
        "cover_node": "close" if short else None,
    }
    return s


class ParityExchange(SwapExchangeMock):
    """Fills at the engine's price (no `average`), fee at FEE_PCT of the
    notional — quote on entries (at `mark`, the candle close the entry fills
    at), base on closes (converted at the fill by `fee_cash`). On the inverse
    contract the entry fee is charged in BTC (= cash) and the close fee in
    USD (= quote, divided by the fill price). Exact amount precision.
    `liquidated=True` makes the exchange reject reduce-only orders and report
    no position. `cash` is the wallet currency `fetch_balance` reports."""

    def __init__(self, cash="USDT", **kw):
        super().__init__(**kw)
        self.markets[INVERSE] = dict(INVERSE_MARKET)
        self.markets[EUR_SPOT] = dict(EUR_MARKET)
        self.cash = cash
        self.mark = None
        self.liquidated = False

    def fetch_balance(self):
        return {self.cash: {"free": self.free_quote, "used": 0.0, "total": self.free_quote},
                "free": {self.cash: self.free_quote}}

    def amount_to_precision(self, symbol, amount):
        return repr(float(amount))

    def fetch_positions(self, symbols=None, params=None):
        return [] if self.liquidated else list(self.positions)

    def _fill(self, side, symbol, amount, reduce_only):
        if reduce_only and self.liquidated:
            raise RuntimeError("ReduceOnly Order is rejected")
        order = self._create(side, symbol, amount)
        if symbol == INVERSE:
            usd = amount * INVERSE_SIZE
            if reduce_only:
                order["fee"] = {"currency": "USD", "cost": usd * FEE_PCT / 100}
            else:
                order["fee"] = {"currency": "BTC", "cost": usd / self.mark * FEE_PCT / 100}
        else:
            base = amount * (CONTRACT_SIZE if symbol == SWAP else 1.0)
            quote = "EUR" if symbol == EUR_SPOT else "USDT"
            if reduce_only:
                order["fee"] = {"currency": "BTC", "cost": base * FEE_PCT / 100}
            else:
                order["fee"] = {"currency": quote, "cost": base * self.mark * FEE_PCT / 100}
        self._orders[order["id"]] = order
        return dict(order)

    def create_market_buy_order(self, symbol, amount):
        return self._fill("buy", symbol, amount, reduce_only=False)

    def create_market_sell_order(self, symbol, amount):
        return self._fill("sell", symbol, amount, reduce_only=True)

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = self._fill(side, symbol, amount, reduce_only=bool((params or {}).get("reduceOnly")))
        self.created[-1]["params"] = dict(params or {})
        return order


@pytest.fixture(autouse=True)
def _fresh_leverage_cache():
    broker._leverage_applied.clear()
    yield
    broker._leverage_applied.clear()


def _manager(monkeypatch, mock=None):
    # The backtest sizes inverse contracts from the registry's market cache
    # (what the poller / an authenticated instance would have loaded)
    monkeypatch.setattr(reg, "_spec_cache", {})
    reg.remember_markets(EXCHANGE, {INVERSE: INVERSE_MARKET, EUR_SPOT: EUR_MARKET})
    bm = BotManager()
    if mock is not None:
        monkeypatch.setattr(bm, "_get_ccxt_instance", lambda key_record, symbol=None: mock)
        real = BotManager._reconcile_order
        monkeypatch.setattr(bm, "_reconcile_order",
                            lambda inst, order, sym, attempts=5, delay=1.0: real(bm, inst, order, sym, attempts, 0))
    return bm


def _tick(bm, symbol, ts):
    asyncio.run(bm._process_bots(EXCHANGE, symbol, TF, ts))


def _trades(db, bot_name, mode):
    """Per position: entry, size, each close fill and the booked result."""
    out = []
    for p in db.query(Position).filter(Position.bot_name == bot_name, Position.mode == mode).order_by(Position.id).all():
        opens = [o for o in p.orders if o.side == pnl.open_order_side(p.side) and o.status == "filled" and not o.reduce_only]
        closes = [o for o in p.orders if o.side == pnl.close_order_side(p.side) and o.status == "filled" and (o.reduce_only or p.market_type in (None, "spot"))]
        closes.sort(key=lambda o: o.id)
        out.append({
            "side": p.side, "status": p.status, "entry": p.entry_price,
            "amount": sum(o.amount for o in opens), "entry_fee": sum(o.fee or 0 for o in opens),
            "closes": [(o.price, o.amount, o.fee or 0.0) for o in closes],
            "profit_abs": p.profit_abs, "profit_pct": p.profit_pct,
            "exits": sorted(p.triggered_exits or []),
        })
    return out


def _wallet_free(db, bot_name, capital=CAPITAL):
    """What an exchange wallet holds after the live fills: capital plus the
    realized PnL (fees netted) minus the margin and entry fee still locked."""
    free = capital
    for p in db.query(Position).filter(Position.bot_name == bot_name, Position.mode == "live").all():
        if p.status == "closed":
            free += p.profit_abs or 0.0
        else:
            free -= pnl.locked_capital(p.entry_price, p.amount, p.leverage or 1,
                                       spec=position_spec(p.symbol, p.contract_kind, p.contract_size, p.exchange))
            free -= sum(o.fee or 0 for o in p.orders if o.side == pnl.open_order_side(p.side) and o.status == "filled")
    return free


def _assert_same_trades(got, want, *, ignore_close_price_on_liquidation=False):
    assert len(got) == len(want), (got, want)
    for g, w in zip(got, want):
        assert g["side"] == w["side"] and g["status"] == w["status"] == "closed"
        assert g["exits"] == w["exits"]
        assert g["entry"] == pytest.approx(w["entry"], rel=1e-9)
        assert g["amount"] == pytest.approx(w["amount"], rel=1e-9)
        assert g["entry_fee"] == pytest.approx(w["entry_fee"], rel=1e-9)
        assert len(g["closes"]) == len(w["closes"])
        for (gp, ga, gf), (wp, wa, wf) in zip(g["closes"], w["closes"]):
            if not (ignore_close_price_on_liquidation and "liquidation" in g["exits"]):
                assert gp == pytest.approx(wp, rel=1e-9)
            assert ga == pytest.approx(wa, rel=1e-9)
            assert gf == pytest.approx(wf, rel=1e-9)
        assert g["profit_abs"] == pytest.approx(w["profit_abs"], rel=1e-9, abs=1e-9)
        assert g["profit_pct"] == pytest.approx(w["profit_pct"], rel=1e-9, abs=1e-9)


def _expected_exits(case):
    liq = case["sl"] == 25
    if liq:
        return [["liquidation"]]
    strategy = "strategy_cover" if case["side"] == "short" else "strategy_sell"
    return [["tp_0"], ["sl_0"], ["sl_0"], [strategy]]


# ── backtest = forward test (fees + slippage) ──────────────────────────────

@pytest.mark.parametrize("name", list(CASES))
def test_forward_test_books_exactly_the_backtest_trades(db, monkeypatch, name):
    case = CASES[name]
    candles = insert_candles(db, _candles(case["symbol"], case["series"]))
    bot = BotConfig(name="parity", is_active=True, is_sandbox=True, strategy="node_graph",
                    settings=_settings(case, slippage=0.5))
    db.add(bot)
    db.commit()

    bm = _manager(monkeypatch)
    bm._execute_sync_backfill(bot.id)  # backtest over the whole series, then live (forward) handover
    db.expire_all()
    assert db.get(BotConfig, bot.id).is_active, db.get(BotConfig, bot.id).settings.get("last_stop_reason")
    for c in candles[WARMUP:]:
        _tick(bm, case["symbol"], c.timestamp)
    db.expire_all()

    backtest = _trades(db, "parity", "backtest")
    forward = _trades(db, "parity", "forward_test")
    assert [t["exits"] for t in backtest] == _expected_exits(case)
    assert all(t["side"] == case["side"] for t in backtest)
    _assert_same_trades(forward, backtest)
    # frictions really were paid: slippage moved the fills, fees were booked
    first = backtest[0]
    assert first["entry_fee"] > 0 and all(f > 0 for (_, _, f) in first["closes"]) or "liquidation" in first["exits"]
    assert first["entry"] == pytest.approx(case["series"][0][3] * (1 - 0.005 if case["side"] == "short" else 1.005))
    summary = db.get(BotConfig, bot.id).settings["last_backtest_summary"]
    assert summary.get("liquidations", 0) == (1 if case["sl"] == 25 else 0)
    # Sprint D: the money unit travels with every record
    cash = case.get("cash", "USDT")
    assert summary["cash_currency"] == cash
    spec = _spec(case)
    for p in db.query(Position).filter(Position.bot_name == "parity").all():
        assert (p.cash_currency, p.contract_kind, p.contract_size) == (cash, spec.kind, spec.contract_size)
    if spec.is_inverse:
        # margin is BTC: 50% of the 1 BTC pool at 5x, sized at the signal
        # candle's close (100) -> 2.5 contracts of 100 USD
        signal_price = case["series"][0][3]
        assert first["amount"] == pytest.approx(0.5 * signal_price / INVERSE_SIZE * 5, rel=1e-9)
        assert spec.margin(first["amount"], signal_price, 5) == pytest.approx(0.5, rel=1e-9)
        assert first["entry_fee"] == pytest.approx(spec.notional_cash(first["amount"], first["entry"]) * FEE_PCT / 100)


# ── backtest = live (mocked exchange, exchange-reported fee, no slippage) ──

@pytest.mark.parametrize("name", list(CASES))
def test_live_books_exactly_the_backtest_trades(db, monkeypatch, name):
    case = CASES[name]
    candles = insert_candles(db, _candles(case["symbol"], case["series"]))
    ref = BotConfig(name="parity-bt", is_active=True, is_sandbox=True, strategy="node_graph",
                    settings=_settings(case, slippage=0))
    db.add(ref)
    db.commit()
    _manager(monkeypatch)._execute_sync_backfill(ref.id)
    db.expire_all()
    ref = db.get(BotConfig, ref.id)
    ref.is_active = False  # the ticks below must not also run it as a forward test
    db.commit()

    db.add(ExchangeKey(name=KEY_NAME, exchange=EXCHANGE, api_key="x", api_secret="y", passphrase="",
                       is_sandbox=False, market_type=case["market"]))
    live = BotConfig(name="parity-live", is_active=True, is_sandbox=False, strategy="node_graph",
                     settings=_settings(case, slippage=0, live_key=KEY_NAME, backtest=False))
    db.add(live)
    db.commit()

    mock = ParityExchange(free_quote=_capital(case), cash=case.get("cash", "USDT"))
    bm = _manager(monkeypatch, mock)
    liq_candle = candles[WARMUP + 1].timestamp if case["sl"] == 25 else None
    for c in candles[WARMUP:]:
        mock.mark = c.close
        mock.free_quote = _wallet_free(db, "parity-live", _capital(case))
        if liq_candle is not None and c.timestamp == liq_candle:
            # The exchange liquidated us intra-candle: the stop's reduce-only
            # close bounces and the position is gone
            mock.liquidated = True
        _tick(bm, case["symbol"], c.timestamp)
        db.expire_all()

    backtest = _trades(db, "parity-bt", "backtest")
    live_trades = _trades(db, "parity-live", "live")
    assert [t["exits"] for t in backtest] == _expected_exits(case)
    _assert_same_trades(live_trades, backtest, ignore_close_price_on_liquidation=True)
    if liq_candle is not None:
        # Documented divergence: live books the liquidation at the candle
        # close (the price the engine had in hand when the exchange said the
        # position was gone); the backtest/forward model books the modelled
        # liquidation price. Loss is margin + entry fee in both.
        (lp, _, lf), = live_trades[0]["closes"]
        (bp, _, bf), = backtest[0]["closes"]
        spec = _spec(case)
        assert lp == pytest.approx(case["series"][1][3]) and lf == 0
        assert bp == pytest.approx(spec.liquidation_price(case["side"], backtest[0]["entry"], case["leverage"])) and bf == 0
        margin = spec.margin(backtest[0]["amount"], backtest[0]["entry"], case["leverage"])
        assert live_trades[0]["profit_abs"] == pytest.approx(-(margin + backtest[0]["entry_fee"]))
    # every live fill went to the exchange and nothing was left open
    assert len(mock.created) == 2 * len(backtest) - (1 if liq_candle is not None else 0)
    assert db.query(Position).filter(Position.bot_name == "parity-live", Position.status == "open").count() == 0
    assert db.query(Order).filter(Order.bot_name == "parity-live", Order.status == "rejected").count() == (1 if liq_candle is not None else 0)
    # Sprint D: live records carry the money unit and what the exchange charged
    cash = case.get("cash", "USDT")
    for p in db.query(Position).filter(Position.bot_name == "parity-live").all():
        assert p.cash_currency == cash and p.contract_kind == _spec(case).kind
    fills = [o for o in db.query(Order).filter(Order.bot_name == "parity-live", Order.status == "filled").all()
             if not str(o.exchange_order_id or "").startswith("liq_")]  # a booked liquidation has no exchange fee
    assert all(o.fee_cash is not None and o.fee_currency for o in fills)
    if case["symbol"] == INVERSE and liq_candle is None:
        # entry fee charged in BTC (= cash, booked as-is), close fee in USD
        # (= quote, converted at the fill price into BTC)
        assert {o.fee_currency for o in fills} == {"BTC", "USD"}
        assert all(o.fee_currency == "USD" and o.fee == pytest.approx(o.fee_cash / o.price) for o in fills if o.reduce_only)
