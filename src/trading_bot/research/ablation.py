"""Ablation: monotone-degradation check.

Plan v4 §13 Tier-1 overfit controls require: "ablation produces monotone
degradation". Given a sequence of (feature_set, score) pairs ordered
from richest to most-stripped, the score series must be non-increasing
(allowing a small tolerance for noise).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class AblationResult:
    monotone: bool
    violations: tuple[tuple[str, float, str, float], ...]
    """Each violation is (richer_name, richer_score, stripped_name,
    stripped_score) where stripped > richer + tolerance — i.e., removing
    features improved the score, which the plan calls overfitting."""


def is_monotone_degradation(
    series: Sequence[tuple[str, float]],
    *,
    tolerance: float = 1e-6,
) -> AblationResult:
    """``series`` is ordered most-rich → most-stripped.

    Returns ``monotone=True`` iff every subsequent score is ≤ the prior
    plus ``tolerance``.
    """
    violations: list[tuple[str, float, str, float]] = []
    if len(series) < 2:
        return AblationResult(monotone=True, violations=())
    prev_name, prev_score = series[0]
    for name, score in series[1:]:
        if score > prev_score + tolerance:
            violations.append((prev_name, prev_score, name, score))
        prev_name, prev_score = name, score
    return AblationResult(
        monotone=not violations,
        violations=tuple(violations),
    )


__all__ = ["AblationResult", "is_monotone_degradation"]
