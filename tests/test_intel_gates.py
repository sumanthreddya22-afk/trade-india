"""Tests for per-trade news/intel gate functions.

Each gate must:
  1. Return a skip-reason string when the gate decides to block
  2. Return None when the gate decides to pass
  3. Return None when the source is unavailable (don't block on intel failure)
  4. Be cache-friendly (single source call across rapid scans)"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from trading_bot import intel_gates
from trading_bot.intelligence import GdeltEvent
from trading_bot.intelligence_finnhub import EarningsRow, FinnhubUnavailable


@pytest.fixture(autouse=True)
def reset_caches():
    intel_gates._reset_caches_for_tests()
    # Reset module-level singletons too so each test gets a fresh client
    intel_gates._finnhub = None
    intel_gates._apewisdom = None
    yield
    intel_gates._reset_caches_for_tests()


# ============================================================================
# Tier 1.1 — Stock earnings gate
# ============================================================================

def test_earnings_gate_returns_none_when_no_earnings_in_window(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin.earnings_calendar.return_value = []
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    assert intel_gates.stock_earnings_gate("AAPL", lookahead_days=5) is None


def test_earnings_gate_blocks_when_earnings_in_window(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin.earnings_calendar.return_value = [
        EarningsRow(symbol="AAPL", date=dt.date.today() + dt.timedelta(days=2),
                    eps_estimate=1.5),
    ]
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    reason = intel_gates.stock_earnings_gate("AAPL", lookahead_days=5)
    assert reason is not None and "earnings" in reason


def test_earnings_gate_no_key_passes(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    fin = MagicMock(); fin.api_key = ""
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    assert intel_gates.stock_earnings_gate("AAPL") is None


def test_earnings_gate_finnhub_unavailable_passes(monkeypatch):
    """API down → don't block trading."""
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin.earnings_calendar.side_effect = FinnhubUnavailable("rate limited")
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    assert intel_gates.stock_earnings_gate("AAPL") is None


def test_earnings_gate_caches_per_symbol_per_day(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin.earnings_calendar.return_value = []
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    intel_gates.stock_earnings_gate("AAPL")
    intel_gates.stock_earnings_gate("AAPL")
    assert fin.earnings_calendar.call_count == 1


# ============================================================================
# Tier 2.2 — Stock insider-cluster gate
# ============================================================================

def test_insider_cluster_gate_blocks_on_5_plus_sells(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin._get.return_value = {"data": [
        {"transactionCode": "S", "name": f"insider_{i}"} for i in range(6)
    ]}
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    reason = intel_gates.stock_insider_cluster_gate("AAPL", sell_volume_threshold=5)
    assert reason is not None and "insider sell cluster" in reason


def test_insider_cluster_gate_passes_on_few_sells(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin._get.return_value = {"data": [
        {"transactionCode": "S", "name": "insider1"},
        {"transactionCode": "S", "name": "insider2"},
    ]}
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    assert intel_gates.stock_insider_cluster_gate("AAPL", sell_volume_threshold=5) is None


def test_insider_cluster_gate_passes_on_buys(monkeypatch):
    """Insider BUYING is a positive signal — never blocks."""
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin._get.return_value = {"data": [
        {"transactionCode": "P", "name": f"insider_{i}"} for i in range(10)
    ]}
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    assert intel_gates.stock_insider_cluster_gate("AAPL") is None


def test_insider_cluster_gate_handles_finnhub_error(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    fin = MagicMock()
    fin.api_key = "fake"
    fin._get.side_effect = Exception("network")
    monkeypatch.setattr(intel_gates, "_get_finnhub", lambda: fin)
    assert intel_gates.stock_insider_cluster_gate("AAPL") is None


# ============================================================================
# Tier 1.2 — Crypto Fear & Greed gate
# ============================================================================

def _fng_resp(value: int):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"data": [{"value": str(value), "value_classification": "Greed"}]}
    m.raise_for_status.return_value = None
    return m


def test_fear_greed_passes_when_in_band():
    with patch("requests.get", return_value=_fng_resp(50)):
        assert intel_gates.crypto_fear_greed_gate(floor=20, ceiling=80) is None


def test_fear_greed_blocks_extreme_fear():
    with patch("requests.get", return_value=_fng_resp(15)):
        reason = intel_gates.crypto_fear_greed_gate(floor=20, ceiling=80)
    assert reason is not None and "fear" in reason


def test_fear_greed_blocks_extreme_greed():
    with patch("requests.get", return_value=_fng_resp(85)):
        reason = intel_gates.crypto_fear_greed_gate(floor=20, ceiling=80)
    assert reason is not None and "greed" in reason


def test_fear_greed_passes_when_source_unavailable():
    with patch("requests.get", side_effect=Exception("net")):
        assert intel_gates.crypto_fear_greed_gate() is None


def test_fear_greed_cached_within_ttl():
    with patch("requests.get", return_value=_fng_resp(50)) as g:
        intel_gates.crypto_fear_greed_gate()
        intel_gates.crypto_fear_greed_gate()
    assert g.call_count == 1


# ============================================================================
# Tier 1.3 — Crypto Reddit-mention spike gate
# ============================================================================

def _ape_resp(rows):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"results": rows}
    m.raise_for_status.return_value = None
    return m


def test_reddit_spike_blocks_on_spike():
    rows = [{"ticker": "BTC", "mentions": 800, "mentions_24h_ago": 200, "rank": 1}]
    with patch("requests.get", return_value=_ape_resp(rows)):
        reason = intel_gates.crypto_reddit_spike_gate("BTC/USD", multiplier=2.0)
    assert reason is not None and "reddit_spike" in reason


def test_reddit_spike_passes_when_quiet():
    rows = [{"ticker": "BTC", "mentions": 220, "mentions_24h_ago": 200, "rank": 1}]
    with patch("requests.get", return_value=_ape_resp(rows)):
        assert intel_gates.crypto_reddit_spike_gate("BTC/USD", multiplier=2.0) is None


def test_reddit_spike_passes_when_symbol_unknown():
    with patch("requests.get", return_value=_ape_resp([])):
        assert intel_gates.crypto_reddit_spike_gate("UNKN/USD") is None


def test_reddit_spike_handles_no_baseline():
    """If ApeWisdom has no 24h-ago count, can't compute spike → don't block."""
    rows = [{"ticker": "NEW", "mentions": 500, "mentions_24h_ago": 0, "rank": 1}]
    with patch("requests.get", return_value=_ape_resp(rows)):
        assert intel_gates.crypto_reddit_spike_gate("NEW/USD") is None


# ============================================================================
# Tier 2.1 — Macro-shock gate (GDELT)
# ============================================================================

def _ge(sentiment: float) -> GdeltEvent:
    return GdeltEvent(title="t", url="u", seendate="d", sourcecountry="US",
                      sentiment=sentiment)


def test_macro_shock_passes_when_sentiment_normal():
    with patch("trading_bot.intelligence.get_gdelt_events",
               return_value=[_ge(0.5), _ge(-1.0), _ge(0.2)]):
        assert intel_gates.macro_shock_gate(threshold=-3.0) is None


def test_macro_shock_blocks_when_sentiment_extreme():
    with patch("trading_bot.intelligence.get_gdelt_events",
               return_value=[_ge(-5.0), _ge(-4.0), _ge(-3.5)]):
        reason = intel_gates.macro_shock_gate(threshold=-3.0)
    assert reason is not None and "macro_shock" in reason


def test_macro_shock_passes_when_source_unavailable():
    with patch("trading_bot.intelligence.get_gdelt_events",
               side_effect=Exception("gdelt down")):
        assert intel_gates.macro_shock_gate() is None


def test_macro_shock_passes_when_no_events():
    with patch("trading_bot.intelligence.get_gdelt_events", return_value=[]):
        assert intel_gates.macro_shock_gate() is None


# ============================================================================
# Tier 2.3 — CoinGecko per-coin community sentiment
# ============================================================================

def _cg_resp(sentiment_pct: float):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"sentiment_votes_up_percentage": sentiment_pct}
    m.raise_for_status.return_value = None
    return m


def test_coingecko_passes_when_sentiment_above_floor():
    with patch("requests.get", return_value=_cg_resp(75.0)):
        assert intel_gates.crypto_coingecko_gate(
            "BTC/USD", sentiment_floor=50.0,
        ) is None


def test_coingecko_blocks_when_sentiment_below_floor():
    with patch("requests.get", return_value=_cg_resp(40.0)):
        reason = intel_gates.crypto_coingecko_gate(
            "BTC/USD", sentiment_floor=50.0,
        )
    assert reason is not None and "coingecko" in reason


def test_coingecko_passes_when_unmapped_symbol():
    """Unmapped coin → don't block (filter-only design)."""
    assert intel_gates.crypto_coingecko_gate("RANDOMCOIN/USD") is None


def test_coingecko_passes_when_source_unavailable():
    with patch("requests.get", side_effect=Exception("net")):
        assert intel_gates.crypto_coingecko_gate("BTC/USD") is None
