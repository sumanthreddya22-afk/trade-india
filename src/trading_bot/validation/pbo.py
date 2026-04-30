"""Probability of Backtest Overfitting (Bailey & López de Prado 2014).

Combinatorially-Symmetric Cross-Validation (CSCV) splits a returns matrix
``M`` of shape ``(n_periods, n_strategies)`` into ``S`` time partitions,
then for every choice of ``S/2`` partitions as "in-sample" (IS), checks
whether the strategy with the highest IS Sharpe ratio falls below median in
out-of-sample (OOS). PBO is the fraction of such combinations where it
does. PBO ≈ 0.5 ↔ no useful IS signal; PBO close to 0 ↔ IS rank predicts OOS.

A defensible promotion gate blocks promotions where ``PBO > 0.5``.

References
----------
Bailey, D. H., Borwein, J. M., López de Prado, M., & Zhu, Q. J. (2014).
"The probability of backtest overfitting." Journal of Computational Finance.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np


def _sharpe(returns_per_strategy: np.ndarray) -> np.ndarray:
    """Sharpe ratio per strategy column. Robust to zero-stdev (returns 0).
    Annualization is irrelevant for ranking — leave returns un-annualized."""
    mu = returns_per_strategy.mean(axis=0)
    sigma = returns_per_strategy.std(axis=0, ddof=1)
    sharpe = np.zeros_like(mu)
    safe = sigma > 0
    sharpe[safe] = mu[safe] / sigma[safe]
    return sharpe


def probability_of_backtest_overfitting(
    returns_matrix: np.ndarray,
    *,
    n_partitions: int = 10,
) -> float:
    """Estimate PBO via CSCV.

    Returns a value in [0, 1]. Higher = more likely overfit.

    - With <2 strategies: returns 0 (no rank ambiguity to overfit on).
    - With < n_partitions periods: returns 1.0 (insufficient data; treat
      as overfit-suspicious so the promotion gate fails closed).
    """
    arr = np.asarray(returns_matrix, dtype=float)
    if arr.ndim != 2:
        raise ValueError(f"returns_matrix must be 2D; got {arr.shape}")
    n_periods, n_strats = arr.shape
    if n_strats < 2:
        return 0.0
    if n_partitions < 2 or n_partitions % 2 != 0:
        raise ValueError("n_partitions must be a positive even integer")
    if n_periods < n_partitions:
        return 1.0  # safe default: too thin → assume overfit

    # Split rows into n_partitions equal-ish chunks (last one absorbs remainder).
    rows = np.arange(n_periods)
    splits = np.array_split(rows, n_partitions)

    # For each combination of S/2 partitions as IS, compute the fraction
    # of cases where the IS-best falls below OOS median.
    n_logit_below = 0
    n_total = 0
    half = n_partitions // 2
    for is_idx in combinations(range(n_partitions), half):
        oos_idx = [i for i in range(n_partitions) if i not in is_idx]
        is_rows = np.concatenate([splits[i] for i in is_idx])
        oos_rows = np.concatenate([splits[i] for i in oos_idx])
        is_sharpe = _sharpe(arr[is_rows, :])
        oos_sharpe = _sharpe(arr[oos_rows, :])
        best = int(np.argmax(is_sharpe))
        # Rank the IS-best in OOS (1 = best, n_strats = worst). Average rank
        # over ties keeps the metric well-defined.
        oos_ranks = (-oos_sharpe).argsort().argsort()  # 0=best, n-1=worst
        rank_of_best_oos = float(oos_ranks[best]) + 1.0  # 1-indexed
        # λ = rank / (N+1). rank=1 means IS-best is also OOS-best (top of
        # the ranking) → λ near 0 → logit(λ) very negative → robust selection.
        # rank near N means IS-best is OOS-worst → λ near 1 → logit(λ) > 0 →
        # the IS rank had no OOS predictive power (overfit).
        # PBO is the fraction of combinations where rank > N/2 (below median).
        if rank_of_best_oos > n_strats / 2.0:
            n_logit_below += 1
        n_total += 1
    return n_logit_below / n_total if n_total else 1.0
