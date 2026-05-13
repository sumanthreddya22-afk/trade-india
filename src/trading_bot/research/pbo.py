"""Probability of Backtest Overfitting (Bailey, Borwein, López de Prado, Zhu, 2016).

Given a returns matrix (N strategies × T periods), split T into
in-sample (IS) and out-of-sample (OOS) halves randomly S times.
Compute the IS-best strategy's OOS rank. PBO = fraction of splits where
the IS-best ranks at-or-below the OOS median. Plan v4 §4 thresholds:
Tier-1 ≤ 0.50, Tier-2 ≤ 0.35, Tier-3 ≤ 0.25.

Reference:
  Bailey, Borwein, López de Prado, Zhu (2016). "The Probability of
  Backtest Overfitting." Journal of Computational Finance.
  https://ssrn.com/abstract=2326253
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    n_splits: int
    n_strategies: int
    n_periods: int


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: Sequence[float], ddof: int = 1) -> float:
    if len(xs) <= ddof:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - ddof))


def _sharpe(xs: Sequence[float]) -> float:
    s = _std(xs)
    if s == 0:
        return 0.0
    return _mean(xs) / s


def _rank(values: Sequence[float]) -> list[int]:
    """Rank-1 = highest. Average-ranks on ties."""
    indexed = sorted(enumerate(values), key=lambda kv: -kv[1])
    n = len(values)
    ranks = [0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-indexed
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = int(round(avg_rank))
        i = j + 1
    return ranks


def probability_of_overfit(
    returns_matrix: Sequence[Sequence[float]],
    *,
    n_splits: int = 16,
    rng_seed: int = 42,
) -> PBOResult:
    """``returns_matrix[strategy][period]`` — N strategies × T periods.

    Splits T into two equal halves (S random partitions); selects the
    IS-best strategy by Sharpe; records its OOS rank; counts the
    fraction at-or-below the OOS median.
    """
    n_strategies = len(returns_matrix)
    if n_strategies < 2:
        raise ValueError("need at least 2 strategies to compute PBO")
    n_periods = len(returns_matrix[0])
    if any(len(r) != n_periods for r in returns_matrix):
        raise ValueError("all strategy return series must have equal length")
    if n_periods < 4:
        raise ValueError("need at least 4 periods to split")

    rng = random.Random(rng_seed)
    indices = list(range(n_periods))
    half = n_periods // 2
    median_rank_threshold = (n_strategies + 1) / 2.0

    below_median = 0
    for _ in range(n_splits):
        shuffled = list(indices)
        rng.shuffle(shuffled)
        is_idx = shuffled[:half]
        oos_idx = shuffled[half:half * 2]
        is_sharpes = [
            _sharpe([returns_matrix[s][t] for t in is_idx])
            for s in range(n_strategies)
        ]
        oos_sharpes = [
            _sharpe([returns_matrix[s][t] for t in oos_idx])
            for s in range(n_strategies)
        ]
        is_best = is_sharpes.index(max(is_sharpes))
        oos_ranks = _rank(oos_sharpes)
        if oos_ranks[is_best] >= median_rank_threshold:
            below_median += 1

    return PBOResult(
        pbo=below_median / n_splits,
        n_splits=n_splits,
        n_strategies=n_strategies,
        n_periods=n_periods,
    )


__all__ = ["PBOResult", "probability_of_overfit"]
