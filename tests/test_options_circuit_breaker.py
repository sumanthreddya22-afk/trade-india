"""Tests for the options circuit breaker (Phase 3).

Covers:
  - Pure evaluator priority ordering when multiple conditions trip.
  - Severity assignment (earnings_cluster = warning, others = hard).
  - Persistence + active-trip detection through cooldown window.
  - auto_clear_expired marks aged trips as cleared.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.circuit_breaker import (
    OptionsBreakerThresholds,
    TripDecision,
    TripReason,
    TripSeverity,
    auto_clear_expired,
    clear,
    evaluate_options_metrics,
    is_tripped,
    trip,
)
from trading_bot.pipelines.options.state_db import CircuitBreakerEventOptions
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Pure evaluator
# ---------------------------------------------------------------------------


def test_no_metrics_means_no_trip():
    decision = evaluate_options_metrics()
    assert decision.should_trip is False
    assert decision.reason is None


def test_vix_spike_trips_hard():
    decision = evaluate_options_metrics(vix_level=42.0)
    assert decision.should_trip is True
    assert decision.reason is TripReason.VIX_SPIKE
    assert decision.severity is TripSeverity.HARD


def test_vix_below_threshold_does_not_trip():
    decision = evaluate_options_metrics(vix_level=20.0)
    assert decision.should_trip is False


def test_term_inversion_trips_when_front_exceeds_back():
    decision = evaluate_options_metrics(
        vix_term_front=22.0, vix_term_back=20.0,
    )
    assert decision.should_trip is True
    assert decision.reason is TripReason.TERM_INVERSION


def test_normal_term_structure_does_not_trip():
    decision = evaluate_options_metrics(
        vix_term_front=18.0, vix_term_back=20.0,
    )
    assert decision.should_trip is False


def test_earnings_cluster_trips_warning():
    decision = evaluate_options_metrics(earnings_cluster_pct=60.0)
    assert decision.should_trip is True
    assert decision.reason is TripReason.EARNINGS_CLUSTER
    assert decision.severity is TripSeverity.WARNING


def test_liquidity_crisis_trips_hard():
    decision = evaluate_options_metrics(median_spread_pct=12.0)
    assert decision.should_trip is True
    assert decision.reason is TripReason.LIQUIDITY


def test_realized_vol_gap_trips():
    decision = evaluate_options_metrics(
        realized_30d_vol=30.0, atm_iv=18.0,  # ratio = 1.67 > 1.5
    )
    assert decision.should_trip is True
    assert decision.reason is TripReason.REALIZED_VOL_GAP


def test_priority_when_multiple_trip():
    """VIX_SPIKE > TERM_INVERSION > REALIZED_VOL_GAP > LIQUIDITY > EARNINGS_CLUSTER."""
    decision = evaluate_options_metrics(
        vix_level=42.0,
        vix_term_front=22.0, vix_term_back=20.0,
        earnings_cluster_pct=60.0,
        median_spread_pct=12.0,
    )
    assert decision.should_trip is True
    assert decision.reason is TripReason.VIX_SPIKE
    # All triggered reasons recorded in state for audit
    assert "options_vix_spike" in decision.state["all_reasons"]
    assert "options_term_inversion" in decision.state["all_reasons"]
    assert "options_liquidity_crisis" in decision.state["all_reasons"]


def test_custom_thresholds_respected():
    th = OptionsBreakerThresholds(vix_spike_level=50.0)
    decision = evaluate_options_metrics(vix_level=42.0, thresholds=th)
    assert decision.should_trip is False


# ---------------------------------------------------------------------------
# Persistence + state queries
# ---------------------------------------------------------------------------


def test_trip_persists_row(engine):
    decision = evaluate_options_metrics(vix_level=42.0)
    row_id = trip(engine, decision=decision)
    assert row_id > 0

    with Session(engine) as session:
        row = session.get(CircuitBreakerEventOptions, row_id)
    assert row is not None
    assert row.reason == "options_vix_spike"
    assert row.severity == "hard"
    assert row.cleared_at is None


def test_trip_raises_when_decision_not_tripped(engine):
    decision = TripDecision(should_trip=False)
    with pytest.raises(ValueError):
        trip(engine, decision=decision)


def test_is_tripped_returns_active_row_within_cooldown(engine):
    now = dt.datetime(2026, 5, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    decision = evaluate_options_metrics(vix_level=42.0)
    trip(engine, decision=decision, cooldown_minutes=60, now=now)

    # 30 minutes later — still tripped
    later = now + dt.timedelta(minutes=30)
    active = is_tripped(engine, now=later)
    assert active is not None
    assert active.reason == "options_vix_spike"


def test_is_tripped_returns_none_after_cooldown(engine):
    now = dt.datetime(2026, 5, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    decision = evaluate_options_metrics(vix_level=42.0)
    trip(engine, decision=decision, cooldown_minutes=60, now=now)

    # 90 minutes later — cooldown elapsed
    later = now + dt.timedelta(minutes=90)
    assert is_tripped(engine, now=later) is None


def test_clear_marks_active_trips(engine):
    decision = evaluate_options_metrics(vix_level=42.0)
    trip(engine, decision=decision)
    trip(engine, decision=decision)
    cleared = clear(engine)
    assert cleared == 2
    assert is_tripped(engine) is None


def test_auto_clear_expired_only_clears_aged(engine):
    now = dt.datetime(2026, 5, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    decision = evaluate_options_metrics(vix_level=42.0)
    # Trip far in past
    trip(engine, decision=decision, cooldown_minutes=60,
         now=now - dt.timedelta(hours=2))
    # Trip recently
    trip(engine, decision=decision, cooldown_minutes=60, now=now)

    cleared = auto_clear_expired(engine, now=now)
    assert cleared == 1
    # Recent one is still active
    active = is_tripped(engine, now=now)
    assert active is not None
