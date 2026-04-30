"""W3.2 — Probability of Backtest Overfitting (Bailey & López de Prado 2014).

Combinatorially-Symmetric Cross-Validation (CSCV) estimates how likely the
"best" strategy (highest in-sample Sharpe) ends up below median in out-of-
sample. PBO close to 0 means the search produced robust strategies; PBO
close to 0.5 means the IS rankings carried no OOS signal (pure overfit).
"""
from __future__ import annotations

import numpy as np
import pytest

from trading_bot.validation.pbo import probability_of_backtest_overfitting


class TestPBO:
    def test_pbo_with_correlated_returns_is_low(self):
        """Strategy 0 always wins; replicated noise → IS leader == OOS leader."""
        rng = np.random.default_rng(seed=42)
        n_periods, n_strats = 200, 50
        # Strategy 0 has true mean 0.01; others have mean 0.
        common_noise = rng.standard_normal((n_periods, 1)) * 0.001
        means = np.zeros(n_strats); means[0] = 0.01
        returns = means[None, :] + common_noise + rng.standard_normal((n_periods, n_strats)) * 0.0005
        pbo = probability_of_backtest_overfitting(returns, n_partitions=10)
        assert 0.0 <= pbo <= 0.3

    def test_pbo_with_pure_noise_is_around_half(self):
        rng = np.random.default_rng(seed=0)
        # All strategies are i.i.d. zero-mean — IS best is essentially random.
        returns = rng.standard_normal((200, 50)) * 0.01
        pbo = probability_of_backtest_overfitting(returns, n_partitions=10)
        # Any value > 0.30 is consistent with the "no signal" hypothesis.
        # The exact value is sensitive to the seed; we just check it's not
        # close to 0 (which would be a false negative on overfitting).
        assert pbo > 0.30

    def test_returns_value_in_unit_interval(self):
        rng = np.random.default_rng(seed=7)
        returns = rng.standard_normal((100, 20)) * 0.005
        pbo = probability_of_backtest_overfitting(returns, n_partitions=8)
        assert 0.0 <= pbo <= 1.0

    def test_too_few_periods_returns_safe_default(self):
        returns = np.zeros((3, 5))
        pbo = probability_of_backtest_overfitting(returns, n_partitions=10)
        # Cannot compute — should return a conservative value (treat as overfit)
        assert pbo == 1.0

    def test_one_strategy_returns_zero(self):
        returns = np.random.default_rng(0).standard_normal((100, 1))
        pbo = probability_of_backtest_overfitting(returns, n_partitions=4)
        # Trivially zero: nothing to be wrong about with one strategy
        assert pbo == 0.0
