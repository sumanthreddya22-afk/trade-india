"""Tests for Phase 3 options intel sources.

Each source is verified at the conversion seam (fetcher → row), with a
fake fetcher so no network calls happen.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.sources.cboe_skew import poll_cboe_skew
from trading_bot.pipelines.options.sources.earnings_calendar import (
    poll_earnings_calendar,
)
from trading_bot.pipelines.options.state_db import IntelEventOptions
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# earnings_calendar
# ---------------------------------------------------------------------------


def test_earnings_calendar_writes_in_window(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    earnings_dates = {
        "AAPL": now + dt.timedelta(days=10),  # in window
        "TSLA": now + dt.timedelta(days=60),  # outside window
    }
    result = poll_earnings_calendar(
        engine, symbols=["AAPL", "TSLA"],
        lookahead_days=45,
        fetcher=lambda s: earnings_dates.get(s),
        now=now,
    )
    assert result.written == 1
    assert result.skipped == 1
    with Session(engine) as session:
        rows = session.query(IntelEventOptions).all()
    assert len(rows) == 1
    assert rows[0].underlying == "AAPL"
    assert rows[0].source == "earnings_calendar"
    assert rows[0].sentiment == -0.3


def test_earnings_calendar_dedup_within_same_day(engine):
    """Re-poll the same day for the same symbol → second call is a no-op."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    fetcher = lambda s: now + dt.timedelta(days=10)
    poll_earnings_calendar(engine, symbols=["AAPL"], fetcher=fetcher, now=now)
    poll_earnings_calendar(engine, symbols=["AAPL"], fetcher=fetcher, now=now)
    with Session(engine) as session:
        rows = session.query(IntelEventOptions).all()
    assert len(rows) == 1


def test_earnings_calendar_skips_past_dates(engine):
    """Earnings already past should be skipped."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    result = poll_earnings_calendar(
        engine, symbols=["AAPL"],
        fetcher=lambda s: now - dt.timedelta(days=2),
        now=now,
    )
    assert result.written == 0
    assert result.skipped == 1


def test_earnings_calendar_handles_fetcher_exceptions(engine):
    """A fetcher that raises must not crash the whole scan."""
    def _bad_fetcher(symbol: str):
        if symbol == "BAD":
            raise RuntimeError("simulated yfinance failure")
        return dt.datetime(2026, 5, 13, tzinfo=dt.timezone.utc)

    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    result = poll_earnings_calendar(
        engine, symbols=["BAD", "GOOD"], fetcher=_bad_fetcher, now=now,
    )
    # GOOD writes; BAD silently skipped.
    assert result.written == 1


def test_earnings_calendar_empty_symbols_returns_zero(engine):
    result = poll_earnings_calendar(engine, symbols=[], fetcher=lambda s: None)
    assert result.written == 0


# ---------------------------------------------------------------------------
# cboe_skew
# ---------------------------------------------------------------------------


def test_cboe_skew_writes_one_row_per_observation(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    observed = dt.datetime(2026, 5, 2, 21, 0, tzinfo=dt.timezone.utc)
    result = poll_cboe_skew(
        engine,
        fetcher=lambda: (148.5, observed),
        now=now,
    )
    assert result.written == 1
    with Session(engine) as session:
        rows = session.query(IntelEventOptions).all()
    assert len(rows) == 1
    assert rows[0].underlying == "SPX"
    assert rows[0].source == "cboe_skew"
    assert rows[0].raw_score == 148.5
    # 148.5 >= 145 → -0.5 sentiment (elevated tail-risk = thinner premium)
    assert rows[0].sentiment == -0.5


def test_cboe_skew_low_skew_positive_sentiment(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    observed = dt.datetime(2026, 5, 2, 21, 0, tzinfo=dt.timezone.utc)
    result = poll_cboe_skew(
        engine, fetcher=lambda: (115.0, observed), now=now,
    )
    assert result.written == 1
    with Session(engine) as session:
        row = session.query(IntelEventOptions).filter_by(source="cboe_skew").one()
    assert row.sentiment == 0.3  # complacent → cheap hedges → wheel-friendly


def test_cboe_skew_handles_fetch_failure(engine):
    """Returns SourceResult with skipped=1 when fetcher returns None."""
    result = poll_cboe_skew(engine, fetcher=lambda: None)
    assert result.written == 0
    assert result.skipped == 1


def test_cboe_skew_dedup_same_day(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    observed = dt.datetime(2026, 5, 2, 21, 0, tzinfo=dt.timezone.utc)
    fetcher = lambda: (148.5, observed)
    poll_cboe_skew(engine, fetcher=fetcher, now=now)
    result = poll_cboe_skew(engine, fetcher=fetcher, now=now)
    assert result.written == 0
    assert result.skipped == 1
    with Session(engine) as session:
        rows = session.query(IntelEventOptions).all()
    assert len(rows) == 1
