"""Tests for crypto-universe discovery — pulls Alpaca's tradable crypto
asset list, filters to USD-quoted, excludes stablecoins, applies operator
blocklist. Replaces the hand-curated watchlist.yaml fallback for crypto."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trading_bot.crypto_universe import (
    STABLECOINS, discover_crypto_universe, is_stablecoin,
)
from trading_bot.alpaca_client import TradableAsset


def _asset(symbol: str, *, tradable: bool = True) -> TradableAsset:
    return TradableAsset(
        symbol=symbol, name=symbol.split("/")[0],
        exchange="CRYPTO", asset_class="crypto",
        tradable=tradable, fractionable=True,
    )


def test_discover_returns_usd_quoted_only():
    ac = MagicMock()
    ac.get_active_assets.return_value = [
        _asset("BTC/USD"),
        _asset("ETH/USD"),
        _asset("BTC/EUR"),  # non-USD
        _asset("ETH/BTC"),  # crypto-quoted
    ]
    out = discover_crypto_universe(ac)
    syms = {e.symbol for e in out}
    assert syms == {"BTC/USD", "ETH/USD"}


def test_discover_excludes_stablecoins():
    ac = MagicMock()
    ac.get_active_assets.return_value = [
        _asset("BTC/USD"),
        _asset("USDC/USD"),  # stablecoin — excluded
        _asset("USDG/USD"),  # stablecoin — excluded
        _asset("USDT/USD"),  # stablecoin — excluded
    ]
    out = discover_crypto_universe(ac)
    syms = {e.symbol for e in out}
    assert "BTC/USD" in syms
    assert syms.isdisjoint({"USDC/USD", "USDG/USD", "USDT/USD"})


def test_discover_excludes_non_tradable():
    ac = MagicMock()
    ac.get_active_assets.return_value = [
        _asset("BTC/USD", tradable=True),
        _asset("DEFUNCT/USD", tradable=False),
    ]
    out = discover_crypto_universe(ac)
    assert {e.symbol for e in out} == {"BTC/USD"}


def test_discover_applies_operator_blocklist():
    ac = MagicMock()
    ac.get_active_assets.return_value = [
        _asset("BTC/USD"),
        _asset("PEPE/USD"),
        _asset("BONK/USD"),
    ]
    out = discover_crypto_universe(ac, blocklist={"PEPE/USD", "BONK/USD"})
    assert {e.symbol for e in out} == {"BTC/USD"}


def test_discover_returns_watchlist_entries_with_crypto_class():
    ac = MagicMock()
    ac.get_active_assets.return_value = [_asset("BTC/USD"), _asset("ETH/USD")]
    out = discover_crypto_universe(ac)
    assert all(e.asset_class == "crypto" for e in out)
    assert all(e.symbol.endswith("/USD") for e in out)


def test_discover_empty_when_alpaca_returns_no_crypto():
    ac = MagicMock()
    ac.get_active_assets.return_value = []
    assert discover_crypto_universe(ac) == []


def test_discover_handles_alpaca_errors_returns_empty():
    """Network/API failure must not crash the bot — return empty list so
    the orchestrator's loop is a clean no-op."""
    ac = MagicMock()
    ac.get_active_assets.side_effect = Exception("alpaca down")
    assert discover_crypto_universe(ac) == []


def test_is_stablecoin_detects_known_names():
    assert is_stablecoin("USDC/USD")
    assert is_stablecoin("USDT/USD")
    assert is_stablecoin("DAI/USD")
    assert is_stablecoin("PAXG/USD")  # gold-backed (non-trading)
    assert not is_stablecoin("BTC/USD")
    assert not is_stablecoin("ETH/USD")


def test_stablecoins_set_is_a_frozenset():
    assert isinstance(STABLECOINS, frozenset)
