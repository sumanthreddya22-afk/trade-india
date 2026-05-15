"""Phase A — intel feature loader + new feed contracts."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from trading_bot import intel as intel_loader
from trading_bot.ingest.intel.crypto_fear_greed import CryptoFearGreedFeed
from trading_bot.ingest.intel.etfdatabase_flows import EtfDatabaseFlowsFeed
from trading_bot.ingest.intel.finra_short_interest import FinraShortInterestFeed
from trading_bot.ingest.intel.treasury_yield_curve import TreasuryYieldCurveFeed


def test_features_for_strategy_reads_policy() -> None:
    price, intel = intel_loader.features_for_strategy("crypto_momentum_v3")
    assert "momentum_30d" in price
    assert "glassnode_mvrv_z" in intel


def test_feed_for_feature_returns_feed_id() -> None:
    # Updated 2026-05-15 — yield curve is sourced from treasury_yield_curve
    # (yfinance) since FRED requires an API key in practice.
    assert intel_loader.feed_for_feature("fred_yield_curve_slope") == "treasury_yield_curve"
    assert intel_loader.feed_for_feature("nonexistent") is None


def test_finra_short_interest_query_returns_none_without_cache(
    tmp_path: Path,
) -> None:
    feed = FinraShortInterestFeed(cache_path=tmp_path / "f.json")
    out = feed.query_features("AAPL", dt.datetime.now(dt.timezone.utc))
    assert out["finra_short_interest_pct"] is None


def test_finra_with_cache(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    p.write_text(json.dumps({
        "published_iso": "2026-05-15T00:00:00Z",
        "series": {"AAPL": {"short_interest_pct": 1.23}},
    }))
    feed = FinraShortInterestFeed(cache_path=p)
    out = feed.query_features("AAPL", dt.datetime.now(dt.timezone.utc))
    assert out["finra_short_interest_pct"] == 1.23


def test_treasury_yield_slope(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "published_iso": "2026-05-15T00:00:00Z",
        "tenors": {"2y": 4.10, "10y": 4.55},
    }))
    feed = TreasuryYieldCurveFeed(cache_path=p)
    out = feed.query_features("ANY", dt.datetime.now(dt.timezone.utc))
    # (4.55 - 4.10) * 100 = 45 bps.
    assert abs(out["fred_yield_curve_slope"] - 45.0) < 1e-6


def test_materialize_features_returns_none_for_missing_feed() -> None:
    out = intel_loader.materialize_features(
        strategy_id="etf_momentum_v3",
        symbol="SPY",
        feed_registry={},  # nothing registered
    )
    # etf_momentum_v3 has intel_features in policy; we get None for each.
    assert "etfdatabase_flow_30d" in out
    assert out["etfdatabase_flow_30d"] is None
    assert out["fred_yield_curve_slope"] is None


def test_materialize_features_uses_registered_feed(tmp_path: Path) -> None:
    p = tmp_path / "etf.json"
    p.write_text(json.dumps({
        "series": {"SPY": {"flow_30d_usd": 1234.5}},
    }))
    feed = EtfDatabaseFlowsFeed(cache_path=p)
    out = intel_loader.materialize_features(
        strategy_id="etf_momentum_v3",
        symbol="SPY",
        feed_registry={"etfdatabase_flows": feed},
    )
    assert out["etfdatabase_flow_30d"] == 1234.5
