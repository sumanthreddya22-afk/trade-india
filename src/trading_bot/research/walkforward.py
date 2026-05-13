"""Walk-forward fold scheduler + locked-holdout split.

Plan v4 §13 Tier-1: walk-forward ≥ 5 folds; locked holdout = last 30%
of history; no parameter changes after holdout. Phase 5 ships the
pure-scheduler math; backtest evaluation itself is the caller's job
(returns get fed into ``robustness_lab.evaluate``).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Fold:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


@dataclass(frozen=True)
class WalkforwardSchedule:
    folds: tuple[Fold, ...]
    holdout_start: dt.date
    holdout_end: dt.date
    holdout_pct: float

    @property
    def n_folds(self) -> int:
        return len(self.folds)


def _add_months(d: dt.date, n: int) -> dt.date:
    """Date arithmetic in months — clamps to month-end when day overflows."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    last_day = (dt.date(year + (month // 12), (month % 12) + 1, 1)
                - dt.timedelta(days=1)).day
    return dt.date(year, month, min(d.day, last_day))


def build_folds(
    *,
    start: dt.date,
    end: dt.date,
    train_months: int = 24,
    test_months: int = 6,
    min_folds: int = 5,
    holdout_pct: float = 0.30,
) -> WalkforwardSchedule:
    """Build a walk-forward schedule + locked holdout.

    The history [start, end] is split: the **last** ``holdout_pct`` is
    locked away; the remaining ``1 - holdout_pct`` is sliced into
    rolling (train, test) folds that march forward by ``test_months``.

    Raises ``ValueError`` if the resulting schedule has fewer than
    ``min_folds`` folds.
    """
    if end <= start:
        raise ValueError("end must be after start")
    if not (0.0 < holdout_pct < 1.0):
        raise ValueError("holdout_pct must be in (0, 1)")

    total_days = (end - start).days
    holdout_days = int(total_days * holdout_pct)
    holdout_start = end - dt.timedelta(days=holdout_days)
    holdout_end = end
    train_end_max = holdout_start

    folds: list[Fold] = []
    # First fold's train window: [start, start + train_months).
    train_start = start
    train_end = _add_months(train_start, train_months)
    while True:
        test_start = train_end
        test_end = _add_months(test_start, test_months)
        if test_end > train_end_max:
            break
        folds.append(Fold(
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
        ))
        train_start = _add_months(train_start, test_months)
        train_end = _add_months(train_start, train_months)

    if len(folds) < min_folds:
        raise ValueError(
            f"only {len(folds)} folds; need {min_folds}. "
            f"Adjust train_months/test_months or extend history."
        )

    return WalkforwardSchedule(
        folds=tuple(folds),
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        holdout_pct=holdout_pct,
    )


__all__ = ["Fold", "WalkforwardSchedule", "build_folds"]
