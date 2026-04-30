"""W2c — Historical VaR + Expected Shortfall engine.

The PDF requires every Decision to record post-trade ``risk_after.portfolio_var_after``
and ``expected_shortfall_after``. This module computes both from a daily-return
history (typically the last 252 trading days of strategy P&L).

Definitions:
- VaR_α: worst α-percentile loss observed (α=0.05 → 95% VaR).
- ES_α: average loss in the worst α-percentile tail (α=0.025 → 97.5% ES).
- Both are returned as POSITIVE numbers expressing loss magnitude as a
  fraction of portfolio equity. e.g., ``var=0.024`` means a 2.4% one-day
  loss at the 95% level.

Robust to small/empty samples — returns 0.0 when the sample is too thin to
estimate, never raises.
"""
from __future__ import annotations

import math

import pytest

from trading_bot.var_engine import (
    expected_shortfall,
    historical_var,
    var_after_trade,
)


class TestHistoricalVaR:
    def test_uniform_losses_var_at_5pct(self):
        # 100 returns all negative, ranging -1% to -100%. The 5%-alpha bucket
        # is the 5 most-negative observations: -100% to -96%. VaR is the
        # boundary (5th-from-worst): -95%, magnitude 0.95.
        returns = [-(i + 1) / 100.0 for i in range(100)]
        v = historical_var(returns, alpha=0.05)
        # idx = int(0.05 * 100) = 5 → sorted[5] = -0.95 → VaR = 0.95
        assert 0.94 <= v <= 0.96

    def test_no_losses_returns_zero(self):
        returns = [0.001, 0.002, 0.003, 0.005]
        v = historical_var(returns, alpha=0.05)
        assert v == 0.0

    def test_empty_returns_zero(self):
        assert historical_var([], alpha=0.05) == 0.0

    def test_single_loss_returns_loss_magnitude(self):
        v = historical_var([-0.10], alpha=0.05)
        assert v == 0.10

    def test_alpha_0_01_more_extreme_than_alpha_0_05(self):
        returns = [-i / 100.0 for i in range(100)]  # -0.00 to -0.99
        v_05 = historical_var(returns, alpha=0.05)
        v_01 = historical_var(returns, alpha=0.01)
        assert v_01 >= v_05  # tighter tail = larger loss number


class TestExpectedShortfall:
    def test_es_greater_than_var(self):
        returns = [-(i + 1) / 100.0 for i in range(20)]  # -0.01 to -0.20
        v = historical_var(returns, alpha=0.05)
        es = expected_shortfall(returns, alpha=0.025)
        assert es > 0
        assert es >= v  # ES is the AVERAGE in the tail, ≥ the percentile

    def test_es_zero_for_no_losses(self):
        es = expected_shortfall([0.01, 0.02, 0.03], alpha=0.025)
        assert es == 0.0

    def test_es_handles_empty(self):
        assert expected_shortfall([], alpha=0.025) == 0.0


class TestVaRAfterTrade:
    def test_zero_addition_returns_baseline(self):
        v_after = var_after_trade(
            current_var=0.024,
            trade_var_contribution=0.0,
        )
        assert math.isclose(v_after, 0.024, rel_tol=1e-9)

    def test_subadditive_combination(self):
        """Trade contribution adds to VaR but not linearly — we use a simple
        additive worst-case here as a defensive upper bound. This is conservative
        (real-world VaR is sub-additive due to correlation), so it's fine for
        a hard gate."""
        v_after = var_after_trade(
            current_var=0.024,
            trade_var_contribution=0.005,
        )
        # 0.024 + 0.005 = 0.029, allowing any sane combination model
        assert v_after == pytest.approx(0.029, rel=1e-6)
