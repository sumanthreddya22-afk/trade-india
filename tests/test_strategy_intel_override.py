"""Phase 6 — strategy_for_regime intel-aware behaviour.

The router used to gate purely on regime — return MomentumStrategy in
trending_up, None everywhere else. Now a high per-ticker intel score
unlocks Momentum in sideways/trending_down too. risk_off remains a hard
wall regardless of intel.
"""
from __future__ import annotations

from trading_bot.strategy import MomentumStrategy, strategy_for_regime


def test_trending_up_returns_momentum_regardless_of_intel():
    """trending_up is always entry-on; intel score doesn't matter."""
    assert isinstance(
        strategy_for_regime("trending_up"), MomentumStrategy
    )
    assert isinstance(
        strategy_for_regime("trending_up", intel_score=0.0), MomentumStrategy
    )
    assert isinstance(
        strategy_for_regime("trending_up", intel_score=20.0), MomentumStrategy
    )


def test_risk_off_is_hard_wall_even_with_huge_intel():
    """risk_off blocks entries unconditionally — intel can't override.
    Catching falling knives on news is exactly what this prevents."""
    assert strategy_for_regime("risk_off") is None
    assert strategy_for_regime("risk_off", intel_score=999.0) is None
    assert strategy_for_regime(
        "risk_off", intel_score=999.0, intel_score_threshold=0.0,
    ) is None


def test_sideways_blocks_when_no_intel():
    """No intel score → no override → no strategy in sideways (legacy)."""
    assert strategy_for_regime("sideways") is None
    assert strategy_for_regime("sideways", intel_score=None) is None


def test_sideways_blocks_when_intel_below_threshold():
    """Below the override threshold → no Momentum."""
    assert strategy_for_regime(
        "sideways", intel_score=4.99, intel_score_threshold=5.0,
    ) is None


def test_sideways_unlocks_when_intel_at_or_above_threshold():
    """At/above the threshold → Momentum returned."""
    s = strategy_for_regime(
        "sideways", intel_score=5.0, intel_score_threshold=5.0,
    )
    assert isinstance(s, MomentumStrategy)
    s2 = strategy_for_regime(
        "sideways", intel_score=8.5, intel_score_threshold=5.0,
    )
    assert isinstance(s2, MomentumStrategy)


def test_trending_down_unlocks_with_high_intel():
    """trending_down behaves like sideways for the override path."""
    assert strategy_for_regime("trending_down") is None
    assert strategy_for_regime(
        "trending_down", intel_score=2.0, intel_score_threshold=5.0,
    ) is None
    s = strategy_for_regime(
        "trending_down", intel_score=7.0, intel_score_threshold=5.0,
    )
    assert isinstance(s, MomentumStrategy)


def test_unknown_regime_returns_none():
    """Defensive: an unrecognised regime string returns None, never crashes."""
    assert strategy_for_regime("zombie_apocalypse") is None
    assert strategy_for_regime(
        "zombie_apocalypse", intel_score=999.0, intel_score_threshold=0.0,
    ) is None
