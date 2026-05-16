"""WS5f Layer 1 — composite 6-signal regime classifier."""
from __future__ import annotations

from trading_bot.risk.composite_regime import assess


def test_all_calm_normal_band() -> None:
    r = assess({
        "vix_percentile": 30, "spy_realized_vol": 40,
        "cross_asset_correlation": 0.5, "market_breadth": 60,
        "credit_spread_velocity": 0.5, "bid_ask_spread": 1.1,
    })
    assert r.band == "normal"
    assert r.size_multiplier == 1.0
    assert r.new_entries_allowed is True
    assert r.halt is False
    assert r.n_signals_stale == 0


def test_vix_spike_elevates() -> None:
    r = assess({
        "vix_percentile": 98, "spy_realized_vol": 92,
        "cross_asset_correlation": 0.9, "market_breadth": 25,
        "credit_spread_velocity": 2.5, "bid_ask_spread": 2.5,
    })
    assert r.band in ("high", "crisis")
    assert r.size_multiplier < 1.0


def test_all_stale_forces_crisis() -> None:
    r = assess({})  # every signal missing
    assert r.n_signals_stale == 6
    assert r.composite_score >= 0.85
    assert r.band == "crisis"
    assert r.halt is True
    assert r.new_entries_allowed is False


def test_two_stale_forces_at_least_high() -> None:
    r = assess({
        "vix_percentile": 30, "spy_realized_vol": 40,
        "cross_asset_correlation": 0.5, "market_breadth": 60,
        # credit + spread missing
    })
    assert r.n_signals_stale == 2
    assert r.composite_score >= 0.7
    assert r.band in ("high", "crisis")


def test_one_stale_forces_elevated() -> None:
    r = assess({
        "vix_percentile": 30, "spy_realized_vol": 40,
        "cross_asset_correlation": 0.5, "market_breadth": 60,
        "credit_spread_velocity": 0.5,
        # bid_ask missing
    })
    assert r.n_signals_stale == 1
    assert r.composite_score >= 0.5
    assert r.band in ("elevated", "high", "crisis")
