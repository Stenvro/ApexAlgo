"""The exchange registry is the single source of truth for which exchanges
exist, how they authenticate and what the connection form shows. Nothing
here touches the network: `build_exchange` only instantiates ccxt classes."""
import os

import ccxt
import pytest
from fastapi.testclient import TestClient

from backend.core import exchange_registry as reg
from backend.main import app

HEADERS = {"X-API-Key": os.environ["MASTER_API_KEY"]}

NEW_EXCHANGES = ["bybit", "gateio", "bitget", "mexc", "htx", "bingx"]


def test_every_spec_is_a_ccxt_exchange_with_consistent_views():
    for ex_id, spec in reg.EXCHANGES.items():
        assert hasattr(ccxt, ex_id), ex_id
        assert spec.name and spec.keys_url, ex_id
        # ccxt's own credential list must agree with the spec, otherwise the
        # key form asks for the wrong fields
        needs = bool(getattr(ccxt, ex_id)().requiredCredentials.get("password"))
        assert needs == spec.needs_passphrase, ex_id
    assert reg.SUPPORTED_EXCHANGES == {k: v.name for k, v in reg.EXCHANGES.items()}
    assert reg._PASSPHRASE_EXCHANGES == {"okx", "kucoin", "bitget"}
    assert set(NEW_EXCHANGES) <= set(reg.EXCHANGES)


def test_build_exchange_applies_spec_and_rejects_unknown():
    for ex_id in NEW_EXCHANGES + ["okx", "kraken"]:
        ex = reg.build_exchange(ex_id, api_key="k", api_secret="s", passphrase="p")
        assert ex.id == ex_id and ex.apiKey == "k"
        assert bool(ex.password) == reg.EXCHANGES[ex_id].needs_passphrase, ex_id
    assert reg.build_exchange("okx").hostname == "eea.okx.com"
    assert reg.exchange_spec("BYBIT").name == "Bybit"
    assert reg.exchange_spec("nope") is None
    with pytest.raises(ValueError, match="not supported"):
        reg.build_exchange("nope")


def test_sandbox_rejected_where_ccxt_has_no_testnet():
    with pytest.raises(ValueError, match="sandbox"):
        reg.build_exchange("mexc", api_key="k", api_secret="s", sandbox=True)
    assert reg.build_exchange("bybit", api_key="k", api_secret="s", sandbox=True) is not None
    # Bitget's demo mode is a request header, not a test URL — must still count
    assert reg.build_exchange("bitget", api_key="k", api_secret="s", passphrase="p", sandbox=True).options["sandboxMode"] is True
    assert reg.exchange_has_sandbox("bitget") and reg.exchange_has_sandbox("bybit")
    assert not reg.exchange_has_sandbox("mexc") and not reg.exchange_has_sandbox("kraken")


def test_market_types_build_the_right_ccxt_class():
    """Phase 2: every exchange has spot; swap only where listed, on the
    derivatives class where ccxt has a separate one, with defaultType set."""
    for ex_id, spec in reg.EXCHANGES.items():
        assert "spot" in spec.markets, ex_id
        for mt, caps in spec.markets.items():
            assert hasattr(ccxt, caps.ccxt_id or ex_id), (ex_id, mt)
            assert caps.max_leverage >= 1
    assert reg.build_exchange("okx").options["defaultType"] == "spot"
    assert reg.build_exchange("bybit").options["defaultType"] == "spot"  # ccxt's own default is swap
    swap = reg.build_exchange("bybit", market_type="swap")
    assert swap.id == "bybit" and swap.options["defaultType"] == "swap"
    assert reg.build_exchange("kucoin", market_type="swap").id == "kucoinfutures"
    assert reg.build_exchange("kraken", market_type="swap").id == "krakenfutures"
    assert reg.build_exchange_for_symbol("bybit", "BTC/USDT:USDT").options["defaultType"] == "swap"
    assert reg.build_exchange_for_symbol("bybit", "BTC/USDT").options["defaultType"] == "spot"
    with pytest.raises(ValueError, match="no 'swap' market"):
        reg.build_exchange("bitvavo", market_type="swap")
    assert reg.market_caps("mexc", "swap") is None  # futures API closed to the public
    assert reg.market_caps("kucoin", "swap").leverage_in_order is True
    assert reg.exchange_has_sandbox("bybit", "swap") and reg.exchange_has_sandbox("kraken", "swap")
    assert not reg.exchange_has_sandbox("htx", "swap") and not reg.exchange_has_sandbox("bitvavo", "swap")

    class Key:
        exchange, name, is_sandbox, api_key, api_secret, passphrase = "bybit", "k", False, "", "", None
    spot_key, swap_key = Key(), Key()
    swap_key.market_type = "swap"
    assert reg._auth_key(spot_key)[3] == "spot" and reg._auth_key(swap_key)[3] == "swap"
    assert reg._auth_key(spot_key) != reg._auth_key(swap_key)


def test_exchanges_endpoint_mirrors_registry():
    r = TestClient(app).get("/api/keys/exchanges", headers=HEADERS)
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert set(rows) == set(reg.EXCHANGES)
    for ex_id, spec in reg.EXCHANGES.items():
        row = rows[ex_id]
        assert row["name"] == spec.name
        assert row["needs_passphrase"] == spec.needs_passphrase
        assert row["keys_url"] == spec.keys_url
        assert row["sandbox_note"] == spec.sandbox_note
        assert isinstance(row["has_sandbox"], bool)
    assert rows["bybit"]["has_sandbox"] is True and rows["bitget"]["has_sandbox"] is True
    assert rows["mexc"]["has_sandbox"] is False and rows["htx"]["has_sandbox"] is False
    # Phase 2: market types per exchange mirror the spec
    for ex_id, spec in reg.EXCHANGES.items():
        assert set(rows[ex_id]["markets"]) == set(spec.markets), ex_id
    assert rows["bitvavo"]["markets"].keys() == {"spot"}
    assert rows["bybit"]["markets"]["swap"]["has_sandbox"] is True
    assert rows["bybit"]["markets"]["swap"]["max_leverage"] >= 1
    assert rows["kucoin"]["markets"]["swap"]["leverage_in_order"] is True
