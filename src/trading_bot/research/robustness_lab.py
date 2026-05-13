"""Robustness lab orchestrator.

Single-entry function that consumes the supplied series + sweeps and
produces a ``RobustnessReport`` with every metric the Tier-N
``registry.evaluate_tier`` evaluator requires.

Plan §13 Tier-1 overfit controls:
  DSR ≥ 0.50, PBO ≤ 0.50, BH-FDR adj. p < 0.10,
  parameter plateau ≥ 25 % of swept range,
  ablation produces monotone degradation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from trading_bot.research.ablation import (
    AblationResult, is_monotone_degradation,
)
from trading_bot.research.dsr import DSRResult, deflated_sharpe
from trading_bot.research.parameter_plateau import (
    PlateauResult, plateau_coverage,
)
from trading_bot.research.pbo import PBOResult, probability_of_overfit


@dataclass(frozen=True)
class RobustnessReport:
    dsr: DSRResult
    pbo: PBOResult
    plateau: PlateauResult
    ablation: AblationResult
    walk_forward_folds: int
    oos_period_days: int
    trades_per_regime: int
    """The minimum trades-per-regime count across all observed regimes."""

    def to_metrics(self) -> dict[str, float]:
        """Bundle into the dict shape expected by
        ``registry.evaluate_tier``."""
        return {
            "oos_dsr": self.dsr.probability_sr_positive,
            "pbo": self.pbo.pbo,
            "walk_forward_folds": float(self.walk_forward_folds),
            "oos_period_days": float(self.oos_period_days),
            "trades_per_regime": float(self.trades_per_regime),
            "_plateau_fraction": self.plateau.plateau_fraction,
            "_ablation_monotone": float(self.ablation.monotone),
        }


def evaluate(
    *,
    primary_returns: Sequence[float],
    cross_section_returns: Sequence[Sequence[float]],
    sweep_metric: Mapping[float, float],
    ablation_series: Sequence[tuple[str, float]],
    walk_forward_folds: int,
    oos_period_days: int,
    trades_per_regime: int,
    n_trials: int = 1,
    variance_trials: float = 1.0,
    plateau_tolerance: float = 0.05,
) -> RobustnessReport:
    """``cross_section_returns`` is the matrix used by PBO: N strategies
    × T periods. Phase 5 callers typically pass the parameter-sweep
    variants as the strategy axis.
    """
    dsr = deflated_sharpe(
        primary_returns, n_trials=n_trials,
        variance_trials=variance_trials,
    )
    pbo = probability_of_overfit(cross_section_returns)
    plateau = plateau_coverage(sweep_metric, tolerance=plateau_tolerance)
    abl = is_monotone_degradation(ablation_series)
    return RobustnessReport(
        dsr=dsr, pbo=pbo, plateau=plateau, ablation=abl,
        walk_forward_folds=walk_forward_folds,
        oos_period_days=oos_period_days,
        trades_per_regime=trades_per_regime,
    )


__all__ = ["RobustnessReport", "evaluate"]
