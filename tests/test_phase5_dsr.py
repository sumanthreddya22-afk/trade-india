"""Phase 5 — Deflated Sharpe Ratio."""
from __future__ import annotations

import random

from trading_bot.research import deflated_sharpe, sharpe_ratio


def test_sharpe_of_zero_returns_is_zero() -> None:
    assert sharpe_ratio([0.0] * 12) == 0.0


def test_sharpe_positive_for_positive_drift() -> None:
    sr = sharpe_ratio([0.01, 0.02, 0.015, 0.018, 0.012])
    assert sr > 0


def test_dsr_short_series_returns_neutral() -> None:
    r = deflated_sharpe([0.01, 0.02])
    assert 0.0 <= r.probability_sr_positive <= 1.0
    assert r.n_obs == 2


def test_dsr_better_with_more_trials_threshold_higher() -> None:
    """The deflation threshold MUST grow with more trials. Same series,
    more trials → higher deflated_sr threshold to beat."""
    rng = random.Random(0)
    returns = [rng.gauss(0.01, 0.02) for _ in range(60)]
    r1 = deflated_sharpe(returns, n_trials=1, variance_trials=1.0)
    r10 = deflated_sharpe(returns, n_trials=10, variance_trials=1.0)
    r100 = deflated_sharpe(returns, n_trials=100, variance_trials=1.0)
    assert r1.deflated_sr <= r10.deflated_sr <= r100.deflated_sr


def test_dsr_positive_for_strong_signal_single_trial() -> None:
    rng = random.Random(0)
    # Clear positive drift, low vol — should produce a high DSR with 1 trial.
    returns = [0.01 + rng.gauss(0, 0.002) for _ in range(60)]
    r = deflated_sharpe(returns, n_trials=1, variance_trials=1.0)
    assert r.probability_sr_positive > 0.8


def test_dsr_drops_for_weak_signal_many_trials() -> None:
    rng = random.Random(0)
    returns = [0.001 + rng.gauss(0, 0.02) for _ in range(60)]
    r1 = deflated_sharpe(returns, n_trials=1, variance_trials=1.0)
    r1000 = deflated_sharpe(returns, n_trials=1000, variance_trials=1.0)
    assert r1.probability_sr_positive > r1000.probability_sr_positive
