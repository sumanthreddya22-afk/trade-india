"""Fitness scoring for backtested strategy variants.

Composite: alpha-over-SPY (multiplicative) + Sortino + drawdown penalty.
Higher is better. Promotion gate is a separate hard threshold check.
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_ALPHA_VS_SPY = 1.5
MIN_SORTINO = 1.0
MAX_DD_PCT = 20.0


@dataclass
class FitnessScore:
    alpha_vs_spy_x: float
    sortino: float
    max_dd_pct: float
    fitness_score: float


def compute_fitness(
    *, alpha_vs_spy_x: float, sortino: float, max_dd_pct: float
) -> FitnessScore:
    dd_penalty = max(0.0, max_dd_pct - MAX_DD_PCT) / 100.0
    fitness = alpha_vs_spy_x + 0.5 * sortino - 0.5 * dd_penalty
    return FitnessScore(
        alpha_vs_spy_x=alpha_vs_spy_x,
        sortino=sortino,
        max_dd_pct=max_dd_pct,
        fitness_score=fitness,
    )


def promotion_gate_check(score: FitnessScore) -> bool:
    return (
        score.alpha_vs_spy_x >= MIN_ALPHA_VS_SPY
        and score.sortino >= MIN_SORTINO
        and score.max_dd_pct <= MAX_DD_PCT
    )
