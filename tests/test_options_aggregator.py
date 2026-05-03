"""Tests for the options aggregator (Phase 3 wiring)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.aggregator import (
    AGG_WINDOW_HOURS,
    event_score,
    roll_up,
    underlying_score,
)
from trading_bot.pipelines.options.sources._base import write_event
from trading_bot.pipelines.options.state_db import (
    IntelCandidateOptions,
    IntelEventOptions,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def test_event_score_decays_with_age():
    fresh = event_score(source="earnings_calendar", sentiment=0.0, age_hours=0.0)
    stale = event_score(source="earnings_calendar", sentiment=0.0, age_hours=72.0)
    assert fresh > stale > 0.0


def test_event_score_sentiment_amplifies():
    neutral = event_score(source="cboe_skew", sentiment=0.0, age_hours=0.0)
    strong = event_score(source="cboe_skew", sentiment=-0.8, age_hours=0.0)
    assert strong > neutral


def test_event_score_unknown_source_uses_default_weight():
    val = event_score(source="invented", sentiment=0.0, age_hours=0.0)
    assert val == pytest.approx(1.0)  # default weight x decay 1.0 x sentiment 1.0


def test_underlying_score_rewards_more_distinct_sources():
    one = underlying_score(sum_event_score=10.0, n_distinct_sources=1)
    three = underlying_score(sum_event_score=10.0, n_distinct_sources=3)
    assert three > one


# ---------------------------------------------------------------------------
# roll_up
# ---------------------------------------------------------------------------


def test_roll_up_no_events_writes_nothing(engine):
    summary = roll_up(engine)
    assert summary["events_considered"] == 0
    assert summary["candidates_upserted"] == 0
    with Session(engine) as session:
        assert session.query(IntelCandidateOptions).count() == 0


def test_roll_up_creates_candidate_per_underlying(engine):
    now = dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.timezone.utc)
    write_event(engine, underlying="AAPL", source="earnings_calendar",
                headline="AAPL earnings in 14d",
                event_at=now + dt.timedelta(days=14),
                sentiment=-0.3, raw_score=14.0,
                event_hash="aapl-1", now=now)
    write_event(engine, underlying="MSFT", source="earnings_calendar",
                headline="MSFT earnings in 7d",
                event_at=now + dt.timedelta(days=7),
                sentiment=-0.3, raw_score=7.0,
                event_hash="msft-1", now=now)
    summary = roll_up(engine, now=now)
    assert summary["candidates_upserted"] == 2
    with Session(engine) as session:
        cands = {c.underlying: c for c in session.query(IntelCandidateOptions).all()}
    assert "AAPL" in cands and "MSFT" in cands
    aapl = cands["AAPL"]
    assert aapl.score > 0
    assert aapl.n_mentions == 1
    assert aapl.earnings_in_dte_window is True
    assert aapl.days_to_earnings == 14


def test_roll_up_marks_earnings_outside_window(engine):
    now = dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.timezone.utc)
    write_event(engine, underlying="TSLA", source="earnings_calendar",
                headline="TSLA earnings 60d out",
                event_at=now + dt.timedelta(days=60),
                sentiment=-0.3,
                event_hash="tsla-1", now=now)
    roll_up(engine, now=now, earnings_dte_window_days=45)
    with Session(engine) as session:
        cand = session.query(IntelCandidateOptions).filter_by(underlying="TSLA").one()
    assert cand.earnings_in_dte_window is False
    assert cand.days_to_earnings is None


def test_roll_up_propagates_cboe_skew_to_all_candidates(engine):
    """CBOE SKEW is index-level — every per-underlying candidate row
    should be tagged with the latest reading."""
    now = dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.timezone.utc)
    # SPX skew event (synthetic underlying)
    write_event(engine, underlying="SPX", source="cboe_skew",
                headline="SKEW 148", event_at=now,
                sentiment=-0.5, raw_score=148.0,
                event_hash="skew-1", now=now)
    write_event(engine, underlying="AAPL", source="earnings_calendar",
                headline="earnings", event_at=now + dt.timedelta(days=10),
                sentiment=-0.3, event_hash="aapl-2", now=now)
    summary = roll_up(engine, now=now)
    assert summary["latest_cboe_skew"] == 148.0
    with Session(engine) as session:
        cand = session.query(IntelCandidateOptions).filter_by(underlying="AAPL").one()
        # SPX shouldn't get a candidate row from skew alone (no per-underlying acc)
        spx = session.query(IntelCandidateOptions).filter_by(underlying="SPX").first()
    assert cand.cboe_skew == 148.0
    assert spx is None


def test_roll_up_excludes_old_events(engine):
    """Events older than AGG_WINDOW_HOURS shouldn't count."""
    now = dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.timezone.utc)
    old = now - dt.timedelta(hours=AGG_WINDOW_HOURS + 24)
    write_event(engine, underlying="AAPL", source="earnings_calendar",
                headline="ancient", event_at=old, sentiment=-0.3,
                event_hash="ancient", now=old)
    summary = roll_up(engine, now=now)
    assert summary["candidates_upserted"] == 0


def test_roll_up_idempotent_upserts_same_row(engine):
    """Running twice on the same events keeps one row per underlying."""
    now = dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.timezone.utc)
    write_event(engine, underlying="AAPL", source="earnings_calendar",
                headline="AAPL earnings", event_at=now + dt.timedelta(days=10),
                sentiment=-0.3, event_hash="aapl-3", now=now)
    roll_up(engine, now=now)
    roll_up(engine, now=now)
    with Session(engine) as session:
        rows = session.query(IntelCandidateOptions).filter_by(underlying="AAPL").all()
    assert len(rows) == 1
