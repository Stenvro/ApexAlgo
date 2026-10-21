"""Audit round 3 (2026-10) — engine-side fixes:

M7  liquidation level with the maintenance margin *and* the fee reserve
    (`contracts.liquidation_price(..., fee=)`), threaded through the backtest;
M11b validator: a percentage stop inside the liquidation band is an error
    for exchange execution, a warning for forward/backtest;
M9  funding coverage reports `partial` + `covered_pct` against the window;
M12 `forward_pool` / `DrawdownTracker.get` never add another currency's PnL;
M13 cross-margin check excludes the layer opened on the candle;
M14 market maps seed the contract-size cache, cold inverse symbols warn;
M17 the drawdown entry block is persisted and re-armed at startup.
"""
from datetime import datetime, timedelta

import pytest

from backend.core import bot_log_buffer as blb
from backend.core import exchange_registry as reg
from backend.engine import funding, pnl, risk, sizing, startup
from backend.engine.backtest import _cross_breached
from backend.engine.bot_manager import BotManager
from backend.engine.contracts import MAINTENANCE_MARGIN, spec_for, spec_from_symbol
from backend.engine.settings_validator import validate_bot_settings
from backend.models.bots import BotConfig
from backend.models.market_data import FundingRate
from backend.models.orders import Order
from backend.models.positions import Position
from tests.conftest import insert_candles, make_candles
from tests.test_live_tick import EXCHANGE, TF, _settings
from tests.test_swap import SWAP, _bt_settings, _run_backtest

LINEAR = spec_from_symbol(SWAP)
INVERSE = spec_from_symbol("BTC/USD:BTC", 100.0)


# ── M7: liquidation level with the fee reserve ─────────────────────────────

def test_liquidation_formula_r3():
    mmr = MAINTENANCE_MARGIN
    assert LINEAR.liquidation_price("long", 100.0, 5, fee=0.001) == pytest.approx(100 * (1 - 1 / 5 + mmr + 0.001))
    assert LINEAR.liquidation_price("short", 100.0, 5, fee=0.001) == pytest.approx(100 * (1 + 1 / 5 - mmr - 0.001))
    assert INVERSE.liquidation_price("long", 100.0, 5, fee=0.001) == pytest.approx(100 * 5 * (1 + mmr + 0.001) / 6)
    assert INVERSE.liquidation_price("short", 100.0, 5, fee=0.001) == pytest.approx(100 * 5 * (1 - mmr - 0.001) / 4)
    # the None rules
    assert LINEAR.liquidation_price("long", 100.0, 1) is None
    assert INVERSE.liquidation_price("short", 100.0, 1) is None
    assert LINEAR.liquidation_price("long", 100.0, 10, mmr=0.1) is None  # band 1/10 − 0.1 = 0
    assert LINEAR.liquidation_price("short", 100.0, 10, mmr=0.15) is None
    # the pnl seam forwards mmr/fee and stays backward compatible without them
    assert pnl.liquidation_price("long", 100.0, 5) == pytest.approx(100 * (1 - 1 / 5 + mmr))
    assert pnl.liquidation_price("long", 100.0, 5, fee=0.001) == LINEAR.liquidation_price("long", 100.0, 5, fee=0.001)
    assert pnl.liquidation_price("long", 100.0, 5, spec=INVERSE, mmr=0.01, fee=0.001) == INVERSE.liquidation_price("long", 100.0, 5, mmr=0.01, fee=0.001)
    assert pnl.liquidation_price("short", 100.0, 1) == pytest.approx(100 * (2 - mmr))


def test_backtest_liquidates_at_the_level_with_the_exit_fee_reserve(db):
    """The backtest passes the bot's configured exit fee (0.1% here) into
    the level: the liquidation order fills at `spec.liquidation_price(...,
    fee=exit_fee)`, closer to the entry than the fee-less level."""
    lev = 10
    bot, positions = _run_backtest(db, _bt_settings(leverage=lev, sl=50, tp=500, amount_pct=20), seed=5)
    liq = [p for p in positions if "liquidation" in (p.triggered_exits or [])]
    assert bot.settings["last_backtest_summary"]["liquidations"] >= 1 and liq
    for p in liq:
        sell = db.query(Order).filter(Order.position_id == p.id, Order.side == "sell").one()
        assert sell.price == pytest.approx(LINEAR.liquidation_price("long", p.entry_price, lev, fee=0.001), rel=1e-9)
        assert sell.price > LINEAR.liquidation_price("long", p.entry_price, lev)  # fee reserve → closer to entry
    console = " ".join(line["msg"] for line in blb.get_logs(bot.name))
    assert "fee reserve" in console and f"{100 * (1 / lev - MAINTENANCE_MARGIN - 0.001):.1f}%" in console


# ── M11b: stop inside the liquidation band ─────────────────────────────────

def _band_settings(lev, sl_pct, live, short=False):
    s = _bt_settings(leverage=lev, sl=sl_pct)
    s["api_execution"] = live
    s["api_key_name"] = "k" if live else None
    s["max_order_value"] = 500
    if short:
        s["trade_settings"]["short"] = {**s["trade_settings"]["entry"],
                                        "stop_losses": [{"type": "percentage", "value": sl_pct, "close_amount_type": "percentage", "close_amount_value": 100}]}
        s["trade_settings"]["entry"]["stop_losses"] = []
        s["nodes"]["short"] = {"class": "condition", "left": "close", "operator": ">", "right": 0}
        s["short_node"] = "short"
    return s


def test_validator_refuses_a_stop_inside_the_liquidation_band_for_exchange_execution():
    # 5x, fee 0.1%: band = 100·(1/5 − 0.005 − 0.001) = 19.4%, 80% of it = 15.52%
    live = validate_bot_settings(_band_settings(5, 16, live=True), exchange_id="binance")
    assert any("inside the liquidation band" in e and "16%" in e and "19.4%" in e and "5x" in e for e in live["errors"]), live
    ok = validate_bot_settings(_band_settings(5, 15, live=True), exchange_id="binance")
    assert not any("liquidation band" in e for e in ok["errors"]), ok["errors"]
    # forward/backtest: a warning, not a refusal
    fwd = validate_bot_settings(_band_settings(5, 16, live=False), exchange_id="binance")
    assert fwd["errors"] == [] and any("inside the liquidation band" in w for w in fwd["warnings"]), fwd
    # the short leg is checked on its own stop, with the "Short " prefix
    sh = validate_bot_settings(_band_settings(5, 18, live=True, short=True), exchange_id="binance")
    assert any(e.startswith("Short stop-loss at 18%") for e in sh["errors"]), sh["errors"]
    # 1x long: no level, nothing to flag; spot: never
    assert not any("liquidation band" in e + "".join(w) for e in validate_bot_settings(_band_settings(1, 60, live=True), exchange_id="binance")["errors"] for w in [""])
    spot = _settings(sl_pct=90)
    assert not any("liquidation band" in x for x in validate_bot_settings(spot, exchange_id="binance")["errors"] + validate_bot_settings(spot, exchange_id="binance")["warnings"])


# ── M9: funding coverage against the window ────────────────────────────────

def test_coverage_reports_partial_with_covered_pct():
    t0 = datetime(2023, 1, 1)
    ev = [(t0 + timedelta(hours=8 * i), 0.0001) for i in range(30)]  # 10 days of settlements
    full = funding.coverage(ev, t0, t0 + timedelta(days=9, hours=16))
    assert full["funding"] == "simulated" and full["covered_pct"] == 100.0
    # the stored range covers the last ~10 of 40 days → partial, 24%
    part = funding.coverage(ev, t0 - timedelta(days=30), t0 + timedelta(days=9, hours=16))
    assert part["funding"] == "partial" and part["covered_pct"] == pytest.approx(100 * (9 + 16 / 24) / (39 + 16 / 24), abs=0.1)
    # missing by less than one settlement interval still counts as covered
    assert funding.coverage(ev, t0 - timedelta(hours=7), ev[-1][0] + timedelta(hours=7))["funding"] == "simulated"
    assert funding.coverage(ev, t0 - timedelta(hours=9), ev[-1][0])["funding"] == "partial"
    assert funding.coverage(ev, None, None)["funding"] == "simulated"
    assert funding.coverage([], t0, t0 + timedelta(days=1)) == {"funding": "no data", "funding_events": 0}


def test_backtest_summary_says_partial_when_funding_covers_the_tail_only(db):
    candles = make_candles(EXCHANGE, SWAP, TF, 400, seed=11, start_price=100.0)
    insert_candles(db, candles)
    # settlements for the last quarter of the window only
    db.add_all([FundingRate(exchange=EXCHANGE, symbol=SWAP, timestamp=c.timestamp, rate=0.0001)
                for i, c in enumerate(candles) if i >= 300 and i % 8 == 0])
    db.commit()
    bot = BotConfig(name="partial-funding", is_active=True, is_sandbox=True, strategy="node_graph", settings=_bt_settings(leverage=3))
    db.add(bot)
    db.commit()
    BotManager()._execute_sync_backfill(bot.id)
    db.expire_all()
    summary = db.query(BotConfig).filter(BotConfig.name == "partial-funding").one().settings["last_backtest_summary"]
    assert summary["funding"] == "partial"
    assert 20 <= summary["funding_covered_pct"] <= 30
    assert summary["funding_by_symbol"][SWAP]["funding"] == "partial"
    assert any("rates stored for" in line["msg"] and "% of the window" in line["msg"] for line in blb.get_logs("partial-funding"))


# ── M12: per-currency realized PnL ─────────────────────────────────────────

def test_forward_pool_and_drawdown_ignore_closes_in_another_currency(db):
    bot = BotConfig(name="fp-bot", is_active=False, is_sandbox=False, strategy="node_graph",
                    settings={"backtest_capital": 1000, "symbols": ["BTC/EUR"]})
    db.add(bot)
    db.add_all([
        Position(exchange="okx", bot_name="fp-bot", symbol="BTC/USDT", mode="forward_test", status="closed", side="long",
                 entry_price=1, amount=1, profit_abs=500.0, cash_currency="USDT", contract_kind="spot", closed_at=datetime(2023, 1, 2)),
        Position(exchange="okx", bot_name="fp-bot", symbol="BTC/EUR", mode="forward_test", status="closed", side="long",
                 entry_price=1, amount=1, profit_abs=-100.0, cash_currency="EUR", contract_kind="spot", closed_at=datetime(2023, 1, 3)),
        # pre-migration row without a currency: still counted (backfilled at startup)
        Position(exchange="okx", bot_name="fp-bot", symbol="ETH/EUR", mode="forward_test", status="closed", side="long",
                 entry_price=1, amount=1, profit_abs=-50.0, cash_currency=None, contract_kind="spot", closed_at=datetime(2023, 1, 4)),
    ])
    db.commit()
    assert sizing.forward_pool(db, bot, "EUR") == pytest.approx(1000 - 100 - 50)
    assert sizing.forward_pool(db, bot, "USDT") == pytest.approx(1000 + 500 - 50)  # the NULL row counts for any currency
    tracker = risk.DrawdownTracker()
    eur = tracker.get("fp-bot", db, mode_group="forward", starting_capital=1000.0, cash_currency="EUR")
    assert eur["running_pnl"] == pytest.approx(-150.0)
    assert eur["max_dd"] == pytest.approx(15.0)
    # without a currency the legacy behaviour (every close) is unchanged
    assert risk.DrawdownTracker().get("fp-bot", db, mode_group="forward", starting_capital=1000.0)["running_pnl"] == pytest.approx(350.0)


# ── M13: cross check excludes the layer opened on the candle ───────────────

class _P:
    def __init__(self, side, entry, amount):
        self.side, self.entry_price, self.amount = side, entry, amount


def test_cross_breach_excludes_the_position_opened_on_the_candle():
    """10x cross, 1000 cash: a long of notional 9000 opened at the close of a
    candle whose low sits 12% under it would 'breach' if marked at that low
    (it did not exist there). Excluded, with its margin + fee back in the
    cash, the account is fine; an older position in the same spot is not."""
    spec = LINEAR
    new = _P("long", 100.0, 90.0)              # notional 9000, margin 900, fee 9 → cash left 91
    ctx = {"spec": spec, "symbol": SWAP, "open_positions": [new], "last_close": 100.0, "tiers": []}
    cash_after_entry = 1000 - 900 - 9
    # treated as an older position: marked at the low → equity 91 + 900 − 1080 < 0 → breach
    assert _cross_breached([ctx], cash_after_entry, 10, ctx, 101.0, 88.0) is not None
    # opened on this candle: excluded, cash restored → no open position to test → no breach
    assert _cross_breached([ctx], cash_after_entry, 10, ctx, 101.0, 88.0, exclude=new, exclude_cost=909.0) is None
    # with another, older layer the test runs on that one alone, against the restored cash
    old = _P("long", 100.0, 50.0)              # notional 5000, margin 500 (+5 fee); the new layer took the rest
    ctx["open_positions"] = [old, new]
    cash = 0.0
    assert _cross_breached([ctx], cash, 10, ctx, 101.0, 88.0, exclude=new, exclude_cost=909.0) is None   # 909 + 500 − 600 > maint
    assert _cross_breached([ctx], cash, 10, ctx, 101.0, 88.0) is not None                                # new included → breach
    breach = _cross_breached([ctx], cash, 10, ctx, 101.0, 1.0, exclude=new, exclude_cost=909.0)
    assert breach == {SWAP: 1.0}                                                                          # old alone is wiped at 1.0


# ── M14: contract sizes from the market map ────────────────────────────────

class _FakeInverseExchange:
    id = "okx"
    has = {"fetchFundingRateHistory": True, "fetchMarketLeverageTiers": False, "fetchLeverageTiers": False}
    markets = {"BTC/USD:BTC": {"symbol": "BTC/USD:BTC", "base": "BTC", "quote": "USD", "settle": "BTC",
                               "swap": True, "contract": True, "inverse": True, "linear": False, "contractSize": 100}}

    def load_markets(self):
        return self.markets

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        return []


def test_fetchers_seed_the_contract_size_cache(monkeypatch):
    monkeypatch.undo()  # drop the conftest stubs of the fetchers for this test only
    monkeypatch.setattr(reg, "_spec_cache", {})
    monkeypatch.setattr(reg, "build_exchange_for_symbol", lambda ex, sym: _FakeInverseExchange())
    assert spec_for("okx", "BTC/USD:BTC").contract_size == 1.0  # cold cache: the symbol's own shape
    assert funding.fetch_history("okx", "BTC/USD:BTC", 0, 1) == []
    assert spec_for("okx", "BTC/USD:BTC").contract_size == 100.0
    assert reg.has_contract_spec("okx", "BTC/USD:BTC")
    monkeypatch.setattr(reg, "_spec_cache", {})
    assert reg.ensure_contract_specs("okx", ["BTC/USD:BTC"]) == []
    assert spec_for("okx", "BTC/USD:BTC").contract_size == 100.0
    # a failing load is logged, never raised; the symbol stays unknown
    monkeypatch.setattr(reg, "_spec_cache", {})
    monkeypatch.setattr(reg, "build_exchange_for_symbol", lambda ex, sym: (_ for _ in ()).throw(RuntimeError("offline")))
    assert reg.ensure_contract_specs("okx", ["BTC/USD:BTC"]) == ["BTC/USD:BTC"]


def test_startup_warns_when_an_inverse_contract_size_is_unknown(db, monkeypatch):
    monkeypatch.setattr(reg, "_spec_cache", {})
    monkeypatch.setattr(startup, "ensure_contract_specs", lambda ex, syms: list(syms))  # no network in tests
    s = _bt_settings(leverage=3, capital=1.0)
    s["symbols"] = ["BTC/USD:BTC"]
    bot, positions = _run_backtest(db, s, symbol="BTC/USD:BTC", name="inv-bot")
    assert any("contract size unknown for BTC/USD:BTC, assuming 1" in line["msg"] and line["level"] == "WARN"
               for line in blb.get_logs("inv-bot"))
    assert positions and all(p.contract_kind == "inverse" and p.contract_size == 1.0 for p in positions)


# ── M17: persisted entry block ─────────────────────────────────────────────

def test_block_persistence_helpers(db):
    bot = BotConfig(name="blk", is_active=False, is_sandbox=True, strategy="node_graph",
                    settings={"drawdown_action": "block_entries", "max_drawdown": 20})
    db.add(bot)
    db.commit()
    assert risk.restore_blocked(bot) is None
    risk.persist_block(db, bot, "drawdown 21.0% > 20%", at=datetime(2026, 10, 1, 12, 0))
    db.commit()
    db.expire_all()
    bot = db.get(BotConfig, bot.id)
    assert bot.settings[risk.BLOCK_KEY] == "2026-10-01T12:00:00" and bot.settings[risk.BLOCK_REASON_KEY] == "drawdown 21.0% > 20%"
    assert risk.restore_blocked(bot) == datetime(2026, 10, 1, 12, 0)
    # close_all bots never re-arm an entry block
    bot.settings = {**bot.settings, "drawdown_action": "close_all"}
    assert risk.restore_blocked(bot) is None
    bot.settings = {**bot.settings, "drawdown_action": "block_entries"}
    risk.clear_block(db, bot)
    db.commit()
    db.expire_all()
    bot = db.get(BotConfig, bot.id)
    assert risk.BLOCK_KEY not in bot.settings and risk.BLOCK_REASON_KEY not in bot.settings
    # the block keys never change the strategy fingerprint
    assert sizing._config_fingerprint({"a": 1}) == sizing._config_fingerprint({"a": 1, risk.BLOCK_KEY: "x", risk.BLOCK_REASON_KEY: "y"})


def test_startup_rearms_a_persisted_entry_block(db):
    s = _bt_settings(leverage=3)
    s.update({"drawdown_action": "block_entries", "max_drawdown": 50, "max_capital_loss": 90,
              risk.BLOCK_KEY: "2026-10-01T12:00:00", risk.BLOCK_REASON_KEY: "drawdown 51.0% > 50%"})
    insert_candles(db, make_candles(EXCHANGE, SWAP, TF, 400, seed=11, start_price=100.0))
    bot = BotConfig(name="rearm", is_active=True, is_sandbox=True, strategy="node_graph", settings=s)
    db.add(bot)
    db.commit()
    bm = BotManager()
    bm._execute_sync_backfill(bot.id)
    db.expire_all()
    assert db.get(BotConfig, bot.id).is_active
    assert "rearm" in bm._entries_blocked
    assert any("Entries still blocked by the drawdown guard since 2026-10-01 12:00" in line["msg"] for line in blb.get_logs("rearm"))
    # a bot that never blocked starts unblocked
    s2 = _bt_settings(leverage=3)
    s2.update({"drawdown_action": "block_entries", "max_drawdown": 50})
    bot2 = BotConfig(name="free", is_active=True, is_sandbox=True, strategy="node_graph", settings=s2)
    db.add(bot2)
    db.commit()
    bm._execute_sync_backfill(bot2.id)
    assert "free" not in bm._entries_blocked
