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


def test_dsr_variance_uses_raw_kurtosis_minus_one() -> None:
    """Regression: Bailey & Lopez de Prado 2014 eq. 4 uses (kurt_raw - 1)/4,
    not (kurt_excess)/4. With normal returns (excess_kurt ≈ 0) and a strong
    SR the bug-form formula underestimates variance, inflating the
    probability above what the correct formula returns. Pin the corrected
    behaviour with an analytic check against a normal series.
    """
    import math

    rng = random.Random(42)
    # 240 monthly-style returns ~ N(0.01, 0.02). Annualised SR ~ 1.7.
    returns = [rng.gauss(0.01, 0.02) for _ in range(240)]
    sr = sharpe_ratio(returns)
    # Recompute variance two ways and confirm the implementation uses
    # the corrected (kurt_excess + 2) term, not just kurt_excess.
    n = len(returns)
    # Skew + excess kurtosis for a finite normal sample are small but
    # non-zero; we only need a stable inequality, not exact values.
    r = deflated_sharpe(returns, n_trials=1, variance_trials=1.0)
    # With the buggy formula, sr_var would drop by 2/4*sr^2 / (n-1)
    # compared to the corrected formula, so dsr_z would be inflated by
    # the corresponding factor. The corrected formula produces a
    # *lower* probability than the buggy one; pin a ceiling.
    buggy_var = max((1.0 - 0.0 * sr + 0.0 / 4.0 * sr * sr) / (n - 1), 1e-12)
    buggy_z = (sr - 0.0) / math.sqrt(buggy_var)
    correct_var = max(
        (1.0 - 0.0 * sr + 2.0 / 4.0 * sr * sr) / (n - 1), 1e-12,
    )
    correct_z = (sr - 0.0) / math.sqrt(correct_var)
    # The corrected z must be strictly smaller (variance larger).
    assert correct_z < buggy_z
    # And the reported probability must be consistent with the
    # corrected (larger) variance, not the bug's smaller one. A
    # tolerance of 0.5 sigma on the inferred z bracket the right form.
    inferred_z_from_prob = math.sqrt(2.0) * _erfinv_safe(
        2.0 * r.probability_sr_positive - 1.0,
    )
    assert abs(inferred_z_from_prob - correct_z) < abs(
        inferred_z_from_prob - buggy_z,
    )


def _erfinv_safe(y: float) -> float:
    """Tiny rational approx of inverse erf, accurate enough for the
    sanity check above. Not exported. Clamps y to avoid infinities at
    the boundary."""
    import math
    y = max(min(y, 0.999999), -0.999999)
    a = 0.147
    ln = math.log(1.0 - y * y)
    first = 2.0 / (math.pi * a) + ln / 2.0
    return math.copysign(
        math.sqrt(math.sqrt(first * first - ln / a) - first), y,
    )
