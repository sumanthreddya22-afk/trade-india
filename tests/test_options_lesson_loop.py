"""Tests for the options lesson loop (Phase 3)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.lesson_loop import (
    OptionsLessonAggregates,
    WinRate,
    aggregate_outcomes,
    latest_lesson_block,
    write_lesson_row,
)
from trading_bot.pipelines.options.state_db import (
    DebateLessonOptions,
    ScoutDebateRunOptions,
    WheelCycleOptions,
    WheelDebateRunOptions,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# WinRate primitive
# ---------------------------------------------------------------------------


def test_winrate_no_data():
    wr = WinRate()
    assert wr.winrate_pct is None
    assert wr.avg_pnl_pct is None


def test_winrate_two_wins_one_loss():
    wr = WinRate()
    wr.add(won=True, pnl_pct=2.0)
    wr.add(won=True, pnl_pct=3.0)
    wr.add(won=False, pnl_pct=-1.0)
    assert wr.n == 3
    assert wr.wins == 2
    assert wr.winrate_pct == pytest.approx(66.66, rel=0.01)
    assert wr.avg_pnl_pct == pytest.approx(4 / 3, rel=0.01)


# ---------------------------------------------------------------------------
# aggregate_outcomes
# ---------------------------------------------------------------------------


def _seed_closed_cycle(
    engine,
    *,
    underlying: str,
    pnl: float,
    iv_rank: float = 50.0,
    chosen_dte: int = 35,
    chosen_structure: str = "csp",
    now: dt.datetime,
) -> int:
    with Session(engine) as session:
        cycle = WheelCycleOptions(
            underlying=underlying,
            state="closed",
            started_at=now - dt.timedelta(days=10),
            ended_at=now - dt.timedelta(days=1),
            initial_csp_strike=180.0,
            cumulative_premium=5.0,
            realized_pnl=pnl,
        )
        session.add(cycle)
        session.commit()
        cycle_id = cycle.id

        debate = WheelDebateRunOptions(
            run_at=now - dt.timedelta(days=10),
            underlying=underlying,
            iv_rank=iv_rank,
            proposed_delta=0.20, proposed_dte_days=chosen_dte,
            proposed_strike=180.0,
            verdict="place", confidence="high",
            chosen_delta=0.22, chosen_dte_days=chosen_dte,
            chosen_structure=chosen_structure,
            judge_reason="ok",
            cycle_id=cycle_id,
            prompt_version="v1",
        )
        session.add(debate)
        session.commit()
    return cycle_id


def test_aggregate_outcomes_buckets_by_iv_rank_band(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    _seed_closed_cycle(engine, underlying="A", pnl=2.0, iv_rank=80.0, now=now)  # high
    _seed_closed_cycle(engine, underlying="B", pnl=-1.0, iv_rank=40.0, now=now)  # mid
    _seed_closed_cycle(engine, underlying="C", pnl=1.0, iv_rank=20.0, now=now)  # low

    result = aggregate_outcomes(engine, lookback_days=14, now=now)
    assert result.n_cycles_closed == 3
    assert result.n_wheel_debates == 3
    assert "high" in result.per_iv_rank_band
    assert "mid" in result.per_iv_rank_band
    assert "low" in result.per_iv_rank_band
    assert result.per_iv_rank_band["high"].wins == 1
    assert result.per_iv_rank_band["mid"].wins == 0
    assert result.per_iv_rank_band["low"].wins == 1


def test_aggregate_outcomes_buckets_by_dte_band(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    _seed_closed_cycle(engine, underlying="A", pnl=2.0, chosen_dte=7, now=now)   # weekly
    _seed_closed_cycle(engine, underlying="B", pnl=3.0, chosen_dte=35, now=now)  # monthly
    _seed_closed_cycle(engine, underlying="C", pnl=-1.0, chosen_dte=90, now=now)  # quarterly

    result = aggregate_outcomes(engine, lookback_days=14, now=now)
    assert "weekly" in result.per_dte_band
    assert "monthly" in result.per_dte_band
    assert "quarterly" in result.per_dte_band


def test_aggregate_outcomes_excludes_old_cycles(engine):
    """Cycles older than lookback should be ignored."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    # Closed 30 days ago — outside 14d window
    with Session(engine) as session:
        cycle = WheelCycleOptions(
            underlying="OLD",
            state="closed",
            started_at=now - dt.timedelta(days=40),
            ended_at=now - dt.timedelta(days=30),
            cumulative_premium=0.0, realized_pnl=10.0,
        )
        session.add(cycle)
        session.commit()
    result = aggregate_outcomes(engine, lookback_days=14, now=now)
    assert result.n_cycles_closed == 0


def test_aggregate_outcomes_counts_scout_debates(engine):
    """Scout debates are counted by run_at, regardless of cycle status."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    with Session(engine) as session:
        for _ in range(3):
            session.add(ScoutDebateRunOptions(
                run_at=now - dt.timedelta(days=2),
                underlying="AAPL",
                verdict="elevate", confidence="high",
                judge_reason="ok",
                prompt_version="v1",
            ))
        session.commit()
    result = aggregate_outcomes(engine, lookback_days=14, now=now)
    assert result.n_scout_debates == 3


# ---------------------------------------------------------------------------
# write_lesson_row + latest_lesson_block
# ---------------------------------------------------------------------------


def test_write_and_read_lesson_block(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    aggs = OptionsLessonAggregates(
        analysis_date=now, lookback_days=14,
        n_cycles_closed=5, n_wheel_debates=10, n_scout_debates=12,
        per_iv_rank_band={
            "high": WinRate(n=3, wins=2, pnl_sum=4.0),
            "mid":  WinRate(n=2, wins=1, pnl_sum=0.5),
        },
        per_structure={"csp": WinRate(n=5, wins=3, pnl_sum=4.5)},
    )
    write_lesson_row(
        engine, aggregates=aggs,
        summary_text="High-IV-rank bucket leading on premium efficiency.",
        candidate_prompt_edits=["Tighten Beatrice on earnings flag"],
        prompt_version="options_lessons/v1",
    )
    with Session(engine) as session:
        row = session.query(DebateLessonOptions).one()
    assert row.n_cycles_closed == 5

    block = latest_lesson_block(engine, now=now)
    assert "5 cycles closed" in block
    assert "high" in block.lower()  # IV rank band
    assert "csp" in block.lower()


def test_latest_lesson_block_placeholder_when_no_data(engine):
    block = latest_lesson_block(engine)
    assert "no fresh" in block.lower()


def test_latest_lesson_block_filters_old_rows(engine):
    """Lesson rows older than max_age_days are ignored."""
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    aggs = OptionsLessonAggregates(
        analysis_date=now - dt.timedelta(days=30),
        lookback_days=14,
    )
    write_lesson_row(
        engine, aggregates=aggs,
        summary_text="old summary",
        candidate_prompt_edits=[],
        prompt_version="v1",
    )
    block = latest_lesson_block(engine, max_age_days=7, now=now)
    assert "no fresh" in block.lower()
