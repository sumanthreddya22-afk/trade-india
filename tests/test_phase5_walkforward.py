"""Phase 5 — walk-forward + locked holdout schedule."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.research import build_folds


def test_default_history_yields_at_least_5_folds() -> None:
    sched = build_folds(
        start=dt.date(2016, 1, 1), end=dt.date(2024, 12, 31),
    )
    assert sched.n_folds >= 5
    # holdout = last 30%
    days_span = (sched.holdout_end - sched.holdout_start).days
    total = (dt.date(2024, 12, 31) - dt.date(2016, 1, 1)).days
    assert abs(days_span / total - 0.30) < 0.01


def test_test_windows_dont_overlap() -> None:
    sched = build_folds(
        start=dt.date(2016, 1, 1), end=dt.date(2024, 12, 31),
        train_months=24, test_months=6,
    )
    for a, b in zip(sched.folds, sched.folds[1:]):
        assert a.test_end <= b.test_start


def test_test_windows_dont_enter_holdout() -> None:
    sched = build_folds(
        start=dt.date(2016, 1, 1), end=dt.date(2024, 12, 31),
        train_months=24, test_months=6,
    )
    for f in sched.folds:
        assert f.test_end <= sched.holdout_start


def test_too_short_history_raises() -> None:
    with pytest.raises(ValueError, match=r"only \d+ folds"):
        build_folds(
            start=dt.date(2023, 1, 1), end=dt.date(2024, 1, 1),
            train_months=24, test_months=6,
        )


def test_bad_holdout_pct_raises() -> None:
    with pytest.raises(ValueError):
        build_folds(start=dt.date(2016, 1, 1),
                    end=dt.date(2024, 1, 1),
                    holdout_pct=0.0)
    with pytest.raises(ValueError):
        build_folds(start=dt.date(2016, 1, 1),
                    end=dt.date(2024, 1, 1),
                    holdout_pct=1.0)


def test_end_before_start_raises() -> None:
    with pytest.raises(ValueError):
        build_folds(start=dt.date(2024, 1, 1), end=dt.date(2020, 1, 1))
