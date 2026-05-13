"""Phase 5 — robustness lab orchestrator."""
from __future__ import annotations

import random

from trading_bot.research import evaluate


def test_evaluate_produces_full_metrics_bundle() -> None:
    rng = random.Random(0)
    primary = [0.01 + rng.gauss(0, 0.005) for _ in range(60)]
    xs = [[0.01 + rng.gauss(0, 0.005) for _ in range(60)] for _ in range(5)]
    sweep = {1.0: 0.4, 2.0: 0.9, 3.0: 0.91, 4.0: 0.5, 5.0: 0.4}
    ablation = [("full", 1.5), ("no-vol", 1.2), ("baseline", 0.5)]

    report = evaluate(
        primary_returns=primary,
        cross_section_returns=xs,
        sweep_metric=sweep,
        ablation_series=ablation,
        walk_forward_folds=6,
        oos_period_days=300,
        trades_per_regime=40,
        n_trials=1, variance_trials=1.0,
    )
    metrics = report.to_metrics()
    assert "oos_dsr" in metrics
    assert "pbo" in metrics
    assert metrics["walk_forward_folds"] == 6
    assert metrics["trades_per_regime"] == 40
    assert metrics["_ablation_monotone"] == 1.0
