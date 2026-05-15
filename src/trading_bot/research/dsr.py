"""Deflated Sharpe Ratio (Bailey & López de Prado, 2014).

Adjusts the observed Sharpe ratio for:
  - non-normality of returns (skew + excess kurtosis)
  - number of trials (multiple-testing penalty)
  - variance of the trial Sharpes

Returns the probability that the true Sharpe is above ``benchmark_sr``
given the observed sample. Plan v4 §4 thresholds:
  Tier-1 ≥ 0.50, Tier-2 ≥ 0.70, Tier-3 ≥ 0.85.

References:
  Bailey, López de Prado (2014). "The Deflated Sharpe Ratio: Correcting
  for Selection Bias, Backtest Overfitting, and Non-Normality."
  https://ssrn.com/abstract=2460551
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

# Euler-Mascheroni constant; appears in the expected-max-Sharpe formula.
_EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class DSRResult:
    observed_sr: float
    deflated_sr: float
    """Threshold the observed Sharpe must beat for DSR > 0.5."""
    probability_sr_positive: float
    n_obs: int


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: Sequence[float], ddof: int = 1) -> float:
    m = _mean(xs)
    if len(xs) <= ddof:
        return 0.0
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - ddof))


def _skew(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = _mean(xs)
    s = _std(xs, ddof=0)
    if s == 0:
        return 0.0
    return sum((x - m) ** 3 for x in xs) / (n * s ** 3)


def _excess_kurtosis(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 4:
        return 0.0
    m = _mean(xs)
    s = _std(xs, ddof=0)
    if s == 0:
        return 0.0
    return sum((x - m) ** 4 for x in xs) / (n * s ** 4) - 3.0


def sharpe_ratio(returns: Sequence[float]) -> float:
    """Annualisation is the caller's responsibility — this is the raw SR
    of the supplied series."""
    if len(returns) < 2:
        return 0.0
    s = _std(returns, ddof=1)
    if s == 0:
        return 0.0
    return _mean(returns) / s


def _expected_max_sharpe(n_trials: int, variance_trials: float) -> float:
    """Bailey-LdP expected maximum SR under H0 of zero-mean trials.

    Approximation:
        E[max SR] ≈ sqrt(var) * ((1 - γ) * Φ⁻¹(1 - 1/N) + γ * Φ⁻¹(1 - 1/(N*e)))
    where Φ⁻¹ is the inverse standard-normal CDF.
    """
    n = max(int(n_trials), 1)
    if n == 1 or variance_trials <= 0:
        return 0.0
    inv_norm = _inverse_normal_cdf
    e = math.e
    return math.sqrt(variance_trials) * (
        (1 - _EULER_GAMMA) * inv_norm(1 - 1.0 / n)
        + _EULER_GAMMA * inv_norm(1 - 1.0 / (n * e))
    )


def _inverse_normal_cdf(p: float) -> float:
    """Beasley-Springer-Moro approximation of Φ⁻¹(p)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0,1)")
    # Coefficients (Beasley-Springer 1977).
    a = (-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00)
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
           (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


def _standard_normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def deflated_sharpe(
    returns: Sequence[float],
    *,
    n_trials: int = 1,
    variance_trials: float = 1.0,
    benchmark_sr: float = 0.0,
) -> DSRResult:
    """Compute DSR. Returns the probability that the *true* SR exceeds
    ``benchmark_sr`` given the observed sample, accounting for trials +
    non-normality.
    """
    sr = sharpe_ratio(returns)
    n = len(returns)
    if n < 4:
        return DSRResult(observed_sr=sr, deflated_sr=sr,
                         probability_sr_positive=0.5, n_obs=n)
    skew = _skew(returns)
    kurt_excess = _excess_kurtosis(returns)
    expected_max = _expected_max_sharpe(n_trials, variance_trials)
    # Variance of estimated Sharpe (Mertens 2002 / Bailey & Lopez de Prado
    # 2014 eq. 4): var(SR) = (1 - skew*SR + (kurt_raw - 1)/4 * SR^2) / (n-1),
    # where kurt_raw is the 4th standardised moment (3 for a normal). Since
    # ``_excess_kurtosis`` returns kurt_raw - 3, the term becomes
    # (kurt_excess + 2)/4. Previous versions used kurt_excess alone, which
    # underestimated variance and made the gate too lenient at high |SR|.
    sr_var = max(
        (1.0 - skew * sr + (kurt_excess + 2.0) / 4.0 * sr * sr) / (n - 1),
        1e-12,
    )
    sr_std = math.sqrt(sr_var)
    dsr_z = (sr - (benchmark_sr + expected_max)) / sr_std
    prob = _standard_normal_cdf(dsr_z)
    # The "deflated_sr" threshold is what observed SR must exceed for
    # the probability to cross 0.5.
    return DSRResult(
        observed_sr=sr,
        deflated_sr=benchmark_sr + expected_max,
        probability_sr_positive=prob,
        n_obs=n,
    )


__all__ = ["DSRResult", "deflated_sharpe", "sharpe_ratio"]
