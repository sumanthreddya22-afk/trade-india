"""W3.3 — Deflated Sharpe Ratio (López de Prado 2014).

DSR adjusts an observed Sharpe for (a) the number of independent trials run
and (b) non-normality of returns. A high observed Sharpe with N=100 trials
deflates much more than the same Sharpe with N=1.

The function returns a probability in [0, 1] that the true Sharpe is greater
than zero given the observation. A defensible promotion gate: require
``DSR > 0.95``.
"""
from __future__ import annotations

import numpy as np
import pytest

from trading_bot.validation.dsr import deflated_sharpe_ratio


class TestDSR:
    def test_high_sharpe_low_trials_passes(self):
        rng = np.random.default_rng(42)
        # 252 daily returns with mean 0.001, vol 0.005 → Sharpe ≈ 3
        returns = rng.normal(0.001, 0.005, 252)
        prob = deflated_sharpe_ratio(returns, n_trials=1)
        assert prob > 0.9

    def test_high_sharpe_many_trials_deflates(self):
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.005, 252)
        # Same observed Sharpe, but pretending we ran 10000 trials → DSR collapses
        prob = deflated_sharpe_ratio(returns, n_trials=10000)
        assert prob < 0.95

    def test_low_sharpe_returns_low_probability(self):
        rng = np.random.default_rng(0)
        returns = rng.normal(0.0, 0.01, 252)  # zero-mean noise
        prob = deflated_sharpe_ratio(returns, n_trials=1)
        assert prob < 0.95

    def test_returns_in_unit_interval(self):
        rng = np.random.default_rng(1)
        returns = rng.normal(0.0005, 0.01, 200)
        prob = deflated_sharpe_ratio(returns, n_trials=50)
        assert 0.0 <= prob <= 1.0

    def test_too_few_returns_returns_zero(self):
        prob = deflated_sharpe_ratio([0.001, 0.002], n_trials=1)
        assert prob == 0.0
