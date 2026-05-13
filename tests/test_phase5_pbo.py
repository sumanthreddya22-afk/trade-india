"""Phase 5 — Probability of Backtest Overfitting."""
from __future__ import annotations

import random

import pytest

from trading_bot.research import probability_of_overfit


def _genuine_strategies_matrix(n=5, t=60):
    """Mix one consistent winner with no-skill peers. PBO should be low."""
    rng = random.Random(0)
    out: list[list[float]] = []
    for s in range(n):
        if s == 0:
            out.append([0.01 + rng.gauss(0, 0.005) for _ in range(t)])
        else:
            out.append([rng.gauss(0, 0.02) for _ in range(t)])
    return out


def _overfit_strategies_matrix(n=10, t=20):
    """Many noise-only strategies; the in-sample winner is random.
    PBO should be high (~0.5)."""
    rng = random.Random(42)
    return [[rng.gauss(0, 0.02) for _ in range(t)] for _ in range(n)]


def test_pbo_low_for_genuine_winner() -> None:
    r = probability_of_overfit(_genuine_strategies_matrix(), n_splits=32)
    assert r.pbo < 0.30


def test_pbo_high_for_pure_noise() -> None:
    r = probability_of_overfit(_overfit_strategies_matrix(), n_splits=64)
    assert r.pbo > 0.3


def test_pbo_rejects_single_strategy() -> None:
    with pytest.raises(ValueError):
        probability_of_overfit([[0.01, 0.02, 0.01, 0.005]])


def test_pbo_rejects_short_history() -> None:
    with pytest.raises(ValueError):
        probability_of_overfit([[0.01], [0.02]])
