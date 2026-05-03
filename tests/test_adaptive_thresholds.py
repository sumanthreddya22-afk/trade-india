"""Phase E — Adaptive Thresholds tests.

Covers:
  * Shadow filter: lookup() ignores shadow rows, list_shadow can see them
  * write_override(shadow=True) persists with shadow=True
  * propose_source_weights honours min-trades floor
  * Proposed weights bounded by [MIN_WEIGHT, MAX_WEIGHT]
  * write_shadow_overrides skips no-op proposals
  * lookup_source_weight returns live override if present, else static fallback
  * Aggregator picks up tuned weight when adaptive override is live
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from trading_bot import adaptive_thresholds, lesson_loop, threshold_overrides
from trading_bot.adaptive_thresholds import (
    MAX_WEIGHT, MIN_WEIGHT, MIN_TRADES_FOR_TUNING, WeightProposal,
)
from trading_bot.intel.aggregator import SOURCE_WEIGHTS, event_score, roll_up
from trading_bot.lesson_loop import OutcomeReport
from trading_bot.state_db import (
    Base, IntelEvent, ThresholdOverride, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Shadow filter on threshold_overrides.lookup
# ---------------------------------------------------------------------------


def test_lookup_skips_shadow_rows(engine):
    threshold_overrides.write_override(
        engine, knob="x", value=1.5, bounds_min=0.5, bounds_max=2.0,
        shadow=True,
    )
    assert threshold_overrides.lookup(engine, knob="x") is None


def test_lookup_returns_live_override(engine):
    threshold_overrides.write_override(
        engine, knob="x", value=1.5, bounds_min=0.5, bounds_max=2.0,
        shadow=False,
    )
    assert threshold_overrides.lookup(engine, knob="x") == 1.5


def test_lookup_prefers_live_over_shadow(engine):
    """When BOTH a shadow and a live row exist, lookup picks the live one."""
    threshold_overrides.write_override(
        engine, knob="x", value=99.0, bounds_min=0.5, bounds_max=200.0,
        shadow=True,
    )
    threshold_overrides.write_override(
        engine, knob="x", value=2.0, bounds_min=0.5, bounds_max=10.0,
        shadow=False,
    )
    # Live (most recent + non-shadow) should win
    assert threshold_overrides.lookup(engine, knob="x") == 2.0


# ---------------------------------------------------------------------------
# propose_source_weights
# ---------------------------------------------------------------------------


def _seed_lesson(engine, *, per_source: dict):
    """Seed a DebateLesson with per_source_winrate_json built from the dict."""
    report = OutcomeReport(lookback_days=14, n_trades_closed=50)
    report.per_source_winrate = per_source
    lesson_loop.write_lesson(
        engine, report=report,
        summary_text="seeded for adaptive_thresholds tests",
    )


def test_propose_returns_empty_without_lesson(engine):
    proposals = adaptive_thresholds.propose_source_weights(engine)
    assert proposals == []


def test_propose_skips_below_min_trades(engine):
    """Sources with n < MIN_TRADES_FOR_TUNING are ignored."""
    _seed_lesson(engine, per_source={
        "sec_8k": {"n": MIN_TRADES_FOR_TUNING - 1, "winrate": 0.9, "avg_pnl_pct": 1.5},
    })
    proposals = adaptive_thresholds.propose_source_weights(engine)
    assert proposals == []


def test_propose_high_winrate_increases_weight(engine):
    _seed_lesson(engine, per_source={
        "sec_8k": {"n": 10, "winrate": 0.9, "avg_pnl_pct": 1.5},
    })
    proposals = adaptive_thresholds.propose_source_weights(engine)
    assert len(proposals) == 1
    p = proposals[0]
    # current sec_8k weight is 5.0; 0.9 winrate → scale 1.4 → 7.0 → clamp to MAX_WEIGHT
    assert p.proposed_weight == MAX_WEIGHT
    assert p.proposed_weight > p.current_weight


def test_propose_low_winrate_decreases_weight(engine):
    _seed_lesson(engine, per_source={
        "googlenews_rss": {"n": 10, "winrate": 0.2, "avg_pnl_pct": -0.3},
    })
    proposals = adaptive_thresholds.propose_source_weights(engine)
    assert len(proposals) == 1
    # Current googlenews_rss weight is 1.0; 0.2 winrate → scale 0.7 → 0.7 (within bounds)
    assert proposals[0].proposed_weight == pytest.approx(0.7, abs=1e-3)


def test_propose_bounds_enforced(engine):
    """Even with extreme winrate, proposed value stays inside [MIN, MAX]."""
    _seed_lesson(engine, per_source={
        "sec_8k": {"n": 50, "winrate": 1.0, "avg_pnl_pct": 5.0},
        "googlenews_rss": {"n": 50, "winrate": 0.0, "avg_pnl_pct": -2.0},
    })
    proposals = adaptive_thresholds.propose_source_weights(engine)
    by_source = {p.source: p for p in proposals}
    assert by_source["sec_8k"].proposed_weight <= MAX_WEIGHT
    assert by_source["googlenews_rss"].proposed_weight >= MIN_WEIGHT


# ---------------------------------------------------------------------------
# write_shadow_overrides
# ---------------------------------------------------------------------------


def test_write_shadow_overrides_persists_shadow_rows(engine):
    proposals = [
        WeightProposal(
            source="sec_8k", current_weight=5.0, proposed_weight=4.0,
            n_trades=10, winrate=0.6, avg_pnl_pct=0.5,
            rationale="test",
        ),
    ]
    n = adaptive_thresholds.write_shadow_overrides(engine, proposals=proposals)
    assert n == 1
    # Verify the row is shadow=True
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(ThresholdOverride).filter(
            ThresholdOverride.knob == "source_weight:sec_8k"
        ).all()
    assert len(rows) == 1
    assert rows[0].shadow is True
    # Live lookup must not see it
    assert threshold_overrides.lookup(engine, knob="source_weight:sec_8k") is None


def test_write_shadow_skips_no_op_proposals(engine):
    """When proposed ≈ current (within 0.05), don't write."""
    proposals = [
        WeightProposal(
            source="sec_8k", current_weight=5.0, proposed_weight=5.02,
            n_trades=10, winrate=0.5, avg_pnl_pct=0.0,
            rationale="no-op",
        ),
    ]
    n = adaptive_thresholds.write_shadow_overrides(engine, proposals=proposals)
    assert n == 0


# ---------------------------------------------------------------------------
# lookup_source_weight
# ---------------------------------------------------------------------------


def test_lookup_source_weight_falls_back_to_static(engine):
    """No override → static SOURCE_WEIGHTS value."""
    out = adaptive_thresholds.lookup_source_weight(engine, "sec_8k")
    assert out == SOURCE_WEIGHTS["sec_8k"]


def test_lookup_source_weight_uses_live_override(engine):
    threshold_overrides.write_override(
        engine, knob="source_weight:sec_8k",
        value=4.0, bounds_min=MIN_WEIGHT, bounds_max=MAX_WEIGHT,
    )
    out = adaptive_thresholds.lookup_source_weight(engine, "sec_8k")
    assert out == 4.0


def test_lookup_source_weight_ignores_shadow(engine):
    threshold_overrides.write_override(
        engine, knob="source_weight:sec_8k",
        value=4.0, bounds_min=MIN_WEIGHT, bounds_max=MAX_WEIGHT,
        shadow=True,
    )
    # Shadow → static fallback
    out = adaptive_thresholds.lookup_source_weight(engine, "sec_8k")
    assert out == SOURCE_WEIGHTS["sec_8k"]


# ---------------------------------------------------------------------------
# event_score with weight_override
# ---------------------------------------------------------------------------


def test_event_score_uses_explicit_weight_override():
    static = event_score(source="sec_8k", sentiment=0.0, age_hours=0.0)
    overridden = event_score(
        source="sec_8k", sentiment=0.0, age_hours=0.0, weight_override=2.0,
    )
    # static uses SOURCE_WEIGHTS["sec_8k"]=5.0; overridden uses 2.0
    assert static == 5.0
    assert overridden == 2.0


def test_event_score_falls_back_when_override_none():
    out = event_score(source="sec_8k", sentiment=0.0, age_hours=0.0, weight_override=None)
    assert out == 5.0


# ---------------------------------------------------------------------------
# roll_up picks up tuned weight
# ---------------------------------------------------------------------------


def test_rollup_uses_tuned_weight_if_live_override_exists(engine):
    """Seed a live override → roll_up should compute scores using the
    tuned weight, not the static one."""
    from sqlalchemy.orm import Session

    # Seed a live override that doubles sec_8k weight to 10
    threshold_overrides.write_override(
        engine, knob="source_weight:sec_8k",
        value=10.0, bounds_min=MIN_WEIGHT, bounds_max=20.0,
    )
    # Seed an event for that source
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(IntelEvent(
            symbol="NVDA", asset_class="stock", source="sec_8k",
            headline="Q3 beat", sentiment=0.0,
            ingested_at=now, event_at=now,
            event_hash="testhash",
        ))
        s.commit()
    summary = roll_up(engine)
    assert summary["events_considered"] == 1
    # Verify the candidate row got the tuned score (weight 10 vs static 5)
    from trading_bot.state_db import IntelCandidate
    with Session(engine) as s:
        row = s.query(IntelCandidate).filter(IntelCandidate.symbol == "NVDA").first()
    # 1 event at sec_8k weight 10 (override) × cross_source_bonus(1)
    # = 10 × (1 + log(2)) ≈ 16.93
    import math
    expected = 10.0 * (1.0 + math.log(2.0))
    assert row.score == pytest.approx(expected, rel=0.01)


# ---------------------------------------------------------------------------
# run_tuning_cycle
# ---------------------------------------------------------------------------


def test_run_tuning_cycle_returns_proposals_and_writes(engine):
    _seed_lesson(engine, per_source={
        "sec_8k": {"n": 10, "winrate": 0.9, "avg_pnl_pct": 1.5},
        "googlenews_rss": {"n": 10, "winrate": 0.2, "avg_pnl_pct": -0.3},
    })
    out = adaptive_thresholds.run_tuning_cycle(engine)
    assert out["n_proposals"] == 2
    assert out["n_shadow_written"] == 2
    # All written rows are shadow
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(ThresholdOverride).all()
    assert all(r.shadow for r in rows)


def test_run_tuning_cycle_empty_when_no_lesson(engine):
    out = adaptive_thresholds.run_tuning_cycle(engine)
    assert out["n_proposals"] == 0
    assert out["n_shadow_written"] == 0
