"""W3.1 — Walk-forward embargo (purged) cross-validation.

A 5-day embargo between train_end and test_start breaks information
leakage from multi-day lookback indicators (RSI_14, MACD, EMA_20).
"""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.walkforward import default_folds


class TestEmbargo:
    def test_zero_embargo_is_contiguous(self):
        folds = default_folds(
            start=dt.date(2024, 1, 1), end=dt.date(2026, 7, 1),
            n_folds=6, embargo_days=0,
        )
        for fold in folds:
            assert fold.test_start == fold.train_end + dt.timedelta(days=1)

    def test_5day_embargo_inserts_gap(self):
        folds = default_folds(
            start=dt.date(2024, 1, 1), end=dt.date(2026, 7, 1),
            n_folds=6, embargo_days=5,
        )
        assert folds  # at least one fold fits
        for fold in folds:
            assert fold.test_start == fold.train_end + dt.timedelta(days=6)
            assert fold.test_start > fold.train_end + dt.timedelta(days=1)

    def test_negative_embargo_raises(self):
        with pytest.raises(ValueError):
            default_folds(
                start=dt.date(2024, 1, 1), end=dt.date(2026, 7, 1),
                embargo_days=-1,
            )

    def test_embargo_does_not_overlap_test_windows(self):
        """Test windows must not overlap each other regardless of embargo."""
        folds = default_folds(
            start=dt.date(2024, 1, 1), end=dt.date(2026, 7, 1),
            n_folds=6, embargo_days=10,
        )
        for prev, curr in zip(folds, folds[1:]):
            assert curr.test_start > prev.test_end
