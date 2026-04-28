"""BacktestEngineerRole tests."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine

from trading_bot.roles.backtest_engineer import BacktestEngineerRole
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _fake_run_result(equity_curve_pct_growth: float = 0.10):
    """Build a fake BacktestRunResult with a trivial equity curve."""
    res = MagicMock()
    res.starting_equity = Decimal("15000")
    res.ending_equity = Decimal(str(15000 * (1 + equity_curve_pct_growth)))
    # Equity curve: 30 daily ticks growing linearly
    days = 30
    eq_curve = []
    for i in range(days):
        eq = Decimal(str(15000 * (1 + equity_curve_pct_growth * i / (days - 1))))
        eq_curve.append((dt.date(2025, 1, 1) + dt.timedelta(days=i), eq))
    res.equity_curve = eq_curve
    res.trades = []
    return res


def test_role_returns_fitness_inputs(engine):
    role = BacktestEngineerRole(engine=engine)
    fake_results = [_fake_run_result(0.05), _fake_run_result(0.08), _fake_run_result(0.06)]
    with (
        patch(
            "trading_bot.roles.backtest_engineer.walk_forward_backtest",
            return_value=fake_results,
        ),
        patch(
            "trading_bot.roles.backtest_engineer._spy_period_return",
            return_value=0.04,
        ),
    ):
        result = role.safe_run(
            ctx={
                "template": "momentum",
                "params": {"rsi_lower": 55.0},
                "start": dt.date(2024, 1, 1),
                "end": dt.date(2026, 7, 1),
                "n_folds": 3,
            }
        )
    assert result.status.value == "ok"
    out = result.outputs
    assert "alpha_vs_spy_x" in out
    assert "sortino" in out
    assert "max_dd_pct" in out
    assert "folds_passed" in out
    assert "folds_total" in out
    assert out["folds_total"] == 3
    # Strategy returned ~6.3% mean vs SPY 4% → alpha ~1.5x
    assert out["alpha_vs_spy_x"] > 1.0


def test_role_handles_zero_spy_return(engine):
    role = BacktestEngineerRole(engine=engine)
    fake_results = [_fake_run_result(0.05)]
    with (
        patch(
            "trading_bot.roles.backtest_engineer.walk_forward_backtest",
            return_value=fake_results,
        ),
        patch(
            "trading_bot.roles.backtest_engineer._spy_period_return",
            return_value=0.0,
        ),
    ):
        result = role.safe_run(
            ctx={
                "template": "momentum",
                "params": {},
                "start": dt.date(2024, 1, 1),
                "end": dt.date(2026, 7, 1),
                "n_folds": 1,
            }
        )
    assert result.status.value == "ok"
    # Defined behavior: SPY returns 0 → alpha is +inf if strategy positive, 0 if negative.
    # We clamp to a finite sentinel.
    assert isinstance(result.outputs["alpha_vs_spy_x"], float)
