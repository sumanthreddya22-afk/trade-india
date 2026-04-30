"""Deflated Sharpe Ratio (López de Prado 2014).

Returns the probability that the true Sharpe ratio is greater than zero,
adjusted for (a) the number of independent trials in the search and (b)
the skew/kurtosis of the realised returns. With many trials the deflation
is severe — a daily Sharpe of 3 over 252 days collapses to a near-50/50
when 10000 strategies were tried.

Reference
---------
López de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting for
Selection Bias, Backtest Overfitting, and Non-Normality."
Journal of Portfolio Management, 40(5), 94–107.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


# Numerical guard for the variance of the maximum-Sharpe estimator.
_EULER_MASCHERONI = 0.5772156649015329


def _normal_cdf(x: float) -> float:
    """Standard-normal CDF using erf (no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _normal_inv_cdf(p: float) -> float:
    """Inverse standard-normal CDF (Beasley-Springer-Moro). Sufficient
    accuracy for DSR's purposes — we only need ~5 decimal places."""
    if not (0.0 < p < 1.0):
        raise ValueError(f"p must be in (0,1); got {p}")
    # Acklam's approximation
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
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2.0 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
           ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    n_trials: int,
) -> float:
    """Probability that the true Sharpe is > 0 given the observation.

    A defensible promotion gate is ``deflated_sharpe_ratio > 0.95``.
    """
    arr = np.asarray(returns, dtype=float)
    if arr.size < 5:
        return 0.0
    mu = arr.mean()
    sigma = arr.std(ddof=1)
    if sigma == 0:
        return 0.0
    sharpe_obs = mu / sigma
    n = float(arr.size)

    # Skew/kurt of returns (third + fourth standardised moments).
    centered = arr - mu
    var = (centered ** 2).sum() / n
    if var == 0:
        return 0.0
    std = math.sqrt(var)
    skew = ((centered ** 3).sum() / n) / (std ** 3)
    kurt = ((centered ** 4).sum() / n) / (std ** 4)  # raw (>=1, =3 for normal)

    # Sharpe-of-the-best estimator under N independent zero-Sharpe trials
    # (Bailey 2014, eq. 6). For N=1 this collapses to 0; for N=10000 it can
    # exceed 4.0 — i.e., we'd need an enormous observed Sharpe to clear the
    # deflated bar.
    n_trials = max(int(n_trials), 1)
    if n_trials == 1:
        sharpe_max_estimate = 0.0
    else:
        z_inv_minus = _normal_inv_cdf(1.0 - 1.0 / n_trials)
        z_inv_minus_e = _normal_inv_cdf(1.0 - 1.0 / (n_trials * math.e))
        sharpe_max_estimate = (
            (1.0 - _EULER_MASCHERONI) * z_inv_minus
            + _EULER_MASCHERONI * z_inv_minus_e
        )

    # Deflated sharpe statistic (eq. 9 in López de Prado 2014).
    denom = math.sqrt(
        max(
            1.0
            - skew * sharpe_obs
            + ((kurt - 1.0) / 4.0) * (sharpe_obs ** 2),
            1e-10,
        )
    )
    z = (sharpe_obs - sharpe_max_estimate) * math.sqrt(n - 1.0) / denom
    return _normal_cdf(z)
