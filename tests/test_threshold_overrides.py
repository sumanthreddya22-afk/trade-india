"""Tests for trading_bot.threshold_overrides — read/write helpers for the
adaptive thresholds table. Schema property guarantees:

  * Most-recent un-expired row for (knob, regime) wins.
  * Stale rows beyond ``max_age_hours`` silently fall back to None.
  * Values are clamped to ``[bounds_min, bounds_max]`` on read AND write.
  * Expired rows (``expires_at <= now``) are excluded.
  * Regime-specific row preferred over regime-agnostic when both fresh.

These guarantees are what hot-path callers (risk_manager, wheel_lane,
chain.py, orchestrator) rely on. If any of them break, the static YAML
fallback is the safety net — but the tests below lock in the read
semantics so the fallback only fires when intended.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.state_db import Base, ThresholdOverride, get_engine
from trading_bot.threshold_overrides import (
    list_active,
    lookup,
    write_override,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


def test_lookup_missing_returns_none(engine):
    assert lookup(engine, knob="per_trade_risk_pct") is None


def test_write_then_lookup_round_trip(engine):
    write_override(
        engine,
        knob="per_trade_risk_pct",
        value=0.7,
        bounds_min=0.5,
        bounds_max=2.0,
    )
    assert lookup(engine, knob="per_trade_risk_pct") == pytest.approx(0.7)


def test_write_clamps_value_to_bounds_above(engine):
    """Even if the writer hands us 99, we clamp to the bound."""
    write_override(
        engine,
        knob="iv_rank_floor",
        value=99.0,
        bounds_min=10.0,
        bounds_max=50.0,
    )
    assert lookup(engine, knob="iv_rank_floor") == pytest.approx(50.0)


def test_write_clamps_value_to_bounds_below(engine):
    write_override(
        engine,
        knob="iv_rank_floor",
        value=-5.0,
        bounds_min=10.0,
        bounds_max=50.0,
    )
    assert lookup(engine, knob="iv_rank_floor") == pytest.approx(10.0)


def test_lookup_picks_most_recent_row(engine):
    now = dt.datetime.now(dt.timezone.utc)
    # Older row first, then a newer one — newer must win.
    write_override(
        engine,
        knob="per_trade_risk_pct",
        value=1.0,
        bounds_min=0.5,
        bounds_max=2.0,
        now=now - dt.timedelta(hours=10),
    )
    write_override(
        engine,
        knob="per_trade_risk_pct",
        value=0.6,
        bounds_min=0.5,
        bounds_max=2.0,
        now=now - dt.timedelta(hours=2),
    )
    assert lookup(engine, knob="per_trade_risk_pct") == pytest.approx(0.6)


def test_stale_rows_fall_back_to_none(engine):
    """Row older than max_age_hours should be invisible to lookup."""
    now = dt.datetime.now(dt.timezone.utc)
    write_override(
        engine,
        knob="iv_rank_floor",
        value=20.0,
        bounds_min=10.0,
        bounds_max=50.0,
        now=now - dt.timedelta(hours=100),
    )
    # Default max_age=36h — 100h is stale.
    assert lookup(engine, knob="iv_rank_floor") is None


def test_explicit_expires_at_in_past_is_ignored(engine):
    """Operator kill-switch: expires_at in the past disables the row."""
    now = dt.datetime.now(dt.timezone.utc)
    write_override(
        engine,
        knob="iv_rank_floor",
        value=20.0,
        bounds_min=10.0,
        bounds_max=50.0,
        expires_at=now - dt.timedelta(hours=1),
    )
    assert lookup(engine, knob="iv_rank_floor") is None


def test_regime_specific_row_preferred_over_agnostic(engine):
    """When both regime-specific and regime-agnostic rows are fresh, the
    regime-specific row wins — tuner can target only ``risk_off`` without
    disturbing the trend regimes."""
    write_override(
        engine,
        knob="per_trade_risk_pct",
        value=1.0,
        bounds_min=0.5,
        bounds_max=2.0,
        regime=None,  # agnostic
    )
    write_override(
        engine,
        knob="per_trade_risk_pct",
        value=0.5,
        bounds_min=0.5,
        bounds_max=2.0,
        regime="risk_off",
    )
    assert lookup(engine, knob="per_trade_risk_pct", regime="risk_off") == pytest.approx(0.5)
    # Different regime falls back to agnostic.
    assert lookup(engine, knob="per_trade_risk_pct", regime="trending_up") == pytest.approx(1.0)


def test_regime_specific_does_not_leak_to_other_regimes(engine):
    """A risk_off override must not be returned when caller asks for
    trending_up; if no agnostic exists, lookup returns None."""
    write_override(
        engine,
        knob="per_trade_risk_pct",
        value=0.5,
        bounds_min=0.5,
        bounds_max=2.0,
        regime="risk_off",
    )
    assert lookup(engine, knob="per_trade_risk_pct", regime="trending_up") is None


def test_lookup_reclamp_protects_against_buggy_row(engine):
    """Even if a bad row escapes the writer's clamp (someone hand-edits
    sqlite), the reader re-clamps so the hot path always sees a value
    in the safe range."""
    # Bypass write_override to stuff an out-of-range value directly.
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        session.add(ThresholdOverride(
            knob="per_trade_risk_pct",
            value=99.0,  # off the rails
            regime=None,
            bounds_min=0.5,
            bounds_max=2.0,
            set_at=now,
            set_by="bug",
            signal_summary="{}",
        ))
        session.commit()
    assert lookup(engine, knob="per_trade_risk_pct") == pytest.approx(2.0)


def test_list_active_dedupes_by_knob_and_regime(engine):
    now = dt.datetime.now(dt.timezone.utc)
    write_override(
        engine, knob="per_trade_risk_pct", value=1.0,
        bounds_min=0.5, bounds_max=2.0,
        now=now - dt.timedelta(hours=10),
    )
    write_override(
        engine, knob="per_trade_risk_pct", value=0.7,
        bounds_min=0.5, bounds_max=2.0,
        now=now - dt.timedelta(hours=2),
    )
    write_override(
        engine, knob="iv_rank_floor", value=25.0,
        bounds_min=10.0, bounds_max=50.0,
    )
    rows = list_active(engine)
    knobs = sorted(r.knob for r in rows)
    assert knobs == ["iv_rank_floor", "per_trade_risk_pct"]
    pt = next(r for r in rows if r.knob == "per_trade_risk_pct")
    # Most recent only (0.7), not the older 1.0 row.
    assert pt.value == pytest.approx(0.7)


def test_list_active_excludes_expired(engine):
    now = dt.datetime.now(dt.timezone.utc)
    write_override(
        engine, knob="iv_rank_floor", value=25.0,
        bounds_min=10.0, bounds_max=50.0,
        expires_at=now - dt.timedelta(minutes=5),
    )
    assert list_active(engine) == []


def test_signal_summary_round_trips_as_json(engine):
    write_override(
        engine,
        knob="per_trade_risk_pct",
        value=0.6,
        bounds_min=0.5,
        bounds_max=2.0,
        signal_summary={"win_rate_30t": 0.32, "n_trades": 30},
    )
    rows = list_active(engine)
    assert len(rows) == 1
    import json as _json
    payload = _json.loads(rows[0].signal_summary)
    assert payload["n_trades"] == 30
    assert payload["win_rate_30t"] == pytest.approx(0.32)


def test_empty_knob_returns_none(engine):
    assert lookup(engine, knob="") is None
