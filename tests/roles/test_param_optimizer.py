"""ParamOptimizerRole tests."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.roles.base import RoleResult, RoleStatus
from trading_bot.roles.param_optimizer import ParamOptimizerRole
from trading_bot.state_db import Base, EvolutionRun, Leaderboard


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _ok_role_result(*, alpha=1.6, sortino=1.2, dd=15.0, folds_passed=6, folds_total=6):
    return RoleResult(
        role_name="backtest_engineer",
        started_at=dt.datetime.now(dt.timezone.utc),
        finished_at=dt.datetime.now(dt.timezone.utc),
        status=RoleStatus.OK,
        latency_ms=10,
        outputs={
            "alpha_vs_spy_x": alpha,
            "sortino": sortino,
            "max_dd_pct": dd,
            "folds_passed": folds_passed,
            "folds_total": folds_total,
        },
    )


def test_param_optimizer_runs_n_trials(engine):
    role = ParamOptimizerRole(engine=engine)
    fake_engineer = MagicMock()
    fake_engineer.safe_run.side_effect = [_ok_role_result() for _ in range(5)]
    with patch(
        "trading_bot.roles.param_optimizer.BacktestEngineerRole",
        return_value=fake_engineer,
    ):
        result = role.safe_run(ctx={"template": "momentum", "n_trials": 5})
    assert result.status == RoleStatus.OK
    assert result.outputs["n_trials"] == 5
    assert fake_engineer.safe_run.call_count == 5


def test_param_optimizer_writes_leaderboard_rows(engine):
    role = ParamOptimizerRole(engine=engine)
    fake_engineer = MagicMock()
    fake_engineer.safe_run.side_effect = [_ok_role_result() for _ in range(3)]
    with patch(
        "trading_bot.roles.param_optimizer.BacktestEngineerRole",
        return_value=fake_engineer,
    ):
        role.safe_run(ctx={"template": "momentum", "n_trials": 3})
    with Session(engine) as s:
        rows = s.query(Leaderboard).all()
    assert len(rows) == 3
    for row in rows:
        assert row.template_name == "momentum"


def test_param_optimizer_writes_evolution_run_summary(engine):
    role = ParamOptimizerRole(engine=engine)
    fake_engineer = MagicMock()
    fake_engineer.safe_run.side_effect = [
        _ok_role_result(alpha=1.6),
        _ok_role_result(alpha=2.1),  # best
        _ok_role_result(alpha=1.8),
    ]
    with patch(
        "trading_bot.roles.param_optimizer.BacktestEngineerRole",
        return_value=fake_engineer,
    ):
        role.safe_run(ctx={"template": "momentum", "n_trials": 3})
    with Session(engine) as s:
        runs = s.query(EvolutionRun).all()
    assert len(runs) == 1
    assert runs[0].n_trials == 3
    assert runs[0].best_fitness is not None


def test_param_optimizer_handles_engineer_errors(engine):
    """A failed trial shouldn't kill the whole run."""
    role = ParamOptimizerRole(engine=engine)
    fake_engineer = MagicMock()
    bad = RoleResult(
        role_name="backtest_engineer",
        started_at=dt.datetime.now(dt.timezone.utc),
        finished_at=dt.datetime.now(dt.timezone.utc),
        status=RoleStatus.ERROR,
        latency_ms=10,
        outputs={},
        error_text="boom",
    )
    fake_engineer.safe_run.side_effect = [bad, _ok_role_result(), _ok_role_result()]
    with patch(
        "trading_bot.roles.param_optimizer.BacktestEngineerRole",
        return_value=fake_engineer,
    ):
        result = role.safe_run(ctx={"template": "momentum", "n_trials": 3})
    assert result.status == RoleStatus.OK
    # Two leaderboard rows (failed trial skipped)
    with Session(engine) as s:
        assert s.query(Leaderboard).count() == 2
