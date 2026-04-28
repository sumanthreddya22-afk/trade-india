"""Walk-forward harness tests."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

from trading_bot.walkforward import (
    FoldDefinition,
    default_folds,
    walk_forward_backtest,
)


def test_default_folds_six_with_room():
    """6 folds (12mo train + 3mo test, walking quarterly) need 30 months range.
    Range 2024-01-01 → 2026-07-01 (=30 months) fits exactly 6 folds."""
    start = dt.date(2024, 1, 1)
    end = dt.date(2026, 7, 1)
    folds = default_folds(start=start, end=end, n_folds=6)
    assert len(folds) == 6
    # First fold: 2024-01..2024-12 train, 2025-01..2025-03 test
    assert folds[0].train_start == dt.date(2024, 1, 1)
    assert folds[0].train_end == dt.date(2024, 12, 31)
    assert folds[0].test_start == dt.date(2025, 1, 1)
    assert folds[0].test_end == dt.date(2025, 3, 31)
    # Last fold: train 2025-04..2026-03, test 2026-04..2026-06
    assert folds[5].test_start == dt.date(2026, 4, 1)
    assert folds[5].test_end == dt.date(2026, 6, 30)


def test_default_folds_truncates_when_range_too_short():
    """If range can't accommodate n_folds, return however many fit."""
    start = dt.date(2024, 1, 1)
    end = dt.date(2026, 1, 1)  # only 24 months — fits 4 folds, not 6
    folds = default_folds(start=start, end=end, n_folds=6)
    assert len(folds) == 4
    assert folds[3].test_end == dt.date(2025, 12, 31)


def test_walk_forward_invokes_simulator_per_fold():
    with patch("trading_bot.walkforward._run_simulator") as mock_sim:
        mock_sim.return_value = MagicMock()  # BacktestRunResult stub
        results = walk_forward_backtest(
            template_name="momentum",
            params={"rsi_lower": 55.0, "rsi_upper": 70.0},
            start=dt.date(2024, 1, 1),
            end=dt.date(2026, 7, 1),
            n_folds=3,
        )
    assert len(results) == 3
    assert mock_sim.call_count == 3


def test_walk_forward_passes_params_to_runner():
    captured: list = []

    def _capture(*, template_name, params, fold):
        captured.append((template_name, params, fold))
        return MagicMock()

    with patch("trading_bot.walkforward._run_simulator", side_effect=_capture):
        walk_forward_backtest(
            template_name="momentum",
            params={"rsi_lower": 58.0, "rsi_upper": 68.0},
            start=dt.date(2024, 1, 1),
            end=dt.date(2026, 7, 1),
            n_folds=2,
        )
    assert captured[0][0] == "momentum"
    assert captured[0][1] == {"rsi_lower": 58.0, "rsi_upper": 68.0}
    assert isinstance(captured[0][2], FoldDefinition)
