"""Leaderboard read/write tests."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.leaderboard import current_best, params_hash, record_run, top_n
from trading_bot.state_db import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    yield s
    s.close()


def test_record_and_top_n(session):
    record_run(
        session,
        template="momentum",
        params={"rsi_lower": 55.0, "rsi_upper": 70.0},
        alpha=1.6,
        sortino=1.2,
        dd=15.0,
        folds_passed=5,
        folds_total=6,
    )
    record_run(
        session,
        template="momentum",
        params={"rsi_lower": 58.0, "rsi_upper": 68.0},
        alpha=1.9,
        sortino=1.5,
        dd=12.0,
        folds_passed=6,
        folds_total=6,
    )
    rows = top_n(session, n=5)
    assert len(rows) == 2
    # Sorted DESC by fitness_score
    assert rows[0].alpha_vs_spy_x == 1.9
    assert rows[0].fitness_score >= rows[1].fitness_score


def test_current_best_returns_top(session):
    record_run(
        session,
        template="momentum",
        params={"a": 1},
        alpha=1.5,
        sortino=1.0,
        dd=18.0,
        folds_passed=4,
        folds_total=6,
    )
    record_run(
        session,
        template="momentum",
        params={"a": 2},
        alpha=2.0,
        sortino=2.0,
        dd=10.0,
        folds_passed=6,
        folds_total=6,
    )
    best = current_best(session)
    assert best is not None
    assert best.alpha_vs_spy_x == 2.0


def test_current_best_none_when_empty(session):
    assert current_best(session) is None


def test_params_hash_stable_across_key_order():
    a = params_hash({"x": 1, "y": 2})
    b = params_hash({"y": 2, "x": 1})
    assert a == b


def test_params_hash_differs_for_different_values():
    assert params_hash({"x": 1}) != params_hash({"x": 2})


def test_record_run_writes_recorded_at(session):
    record_run(
        session,
        template="momentum",
        params={"x": 1},
        alpha=1.6,
        sortino=1.0,
        dd=10.0,
        folds_passed=6,
        folds_total=6,
    )
    rows = top_n(session, n=1)
    assert rows[0].recorded_at is not None
    assert isinstance(rows[0].recorded_at, dt.datetime)
