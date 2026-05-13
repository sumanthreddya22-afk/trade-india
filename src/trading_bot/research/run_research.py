"""End-to-end driver: hypothesis intake + robustness lab + validation_artifact.

This is the function the operator (or, in Phase 6, the mutation engine)
calls to run one research cycle for a strategy candidate.

For Phase 5 the driver consumes injected P&L series + sweep metrics —
real market-data wiring is a separate operator step.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from trading_bot.registry import (
    TIER_RESEARCH, record_validation_artifact,
)
from trading_bot.research.failure_memory import (
    is_blocked, record_rejection,
)
from trading_bot.research.hypothesis_intake import (
    HypothesisProposal, IntakeResult, PersonaRunnerT, run_intake,
)
from trading_bot.research.robustness_lab import RobustnessReport, evaluate


@dataclass(frozen=True)
class ResearchCycleResult:
    intake: IntakeResult
    report: Optional[RobustnessReport]
    artifact_id: Optional[str]
    artifact_passed: Optional[bool]
    blocked_by_failure_memory: bool = False
    blocked_reason: str = ""


def run_cycle(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_ver: int,
    hypothesis: HypothesisProposal,
    research_lead_runner: PersonaRunnerT,
    risk_validator_runner: PersonaRunnerT,
    policy_hash: str,
    feature_snapshot_id: str,
    validation_policy_lock: Mapping,
    code_hash: str,
    config_hash: str,
    primary_returns: Sequence[float],
    cross_section_returns: Sequence[Sequence[float]],
    sweep_metric: Mapping[float, float],
    ablation_series: Sequence[tuple[str, float]],
    walk_forward_folds: int,
    oos_period_days: int,
    trades_per_regime: int,
    n_trials: int = 1,
    variance_trials: float = 1.0,
    operator_override: bool = False,
    now: Optional[dt.datetime] = None,
) -> ResearchCycleResult:
    now = now or dt.datetime.now(dt.timezone.utc)

    hypothesis_hash = hypothesis.hash()

    blocked, reason = is_blocked(
        conn, hypothesis_hash=hypothesis_hash, now=now,
    )
    if blocked:
        return ResearchCycleResult(
            intake=IntakeResult(
                accepted=False, hypothesis_hash=hypothesis_hash,
                research_lead_output={}, risk_validator_output={},
                reason=f"failure_memory:{reason}",
            ),
            report=None, artifact_id=None, artifact_passed=None,
            blocked_by_failure_memory=True, blocked_reason=reason or "",
        )

    intake = run_intake(
        conn,
        strategy_id=strategy_id, strategy_ver=strategy_ver,
        hypothesis=hypothesis,
        research_lead_runner=research_lead_runner,
        risk_validator_runner=risk_validator_runner,
        policy_hash=policy_hash,
        feature_snapshot_id=feature_snapshot_id,
        operator_override=operator_override, now=now,
    )
    if not intake.accepted:
        record_rejection(
            conn, hypothesis_hash=hypothesis_hash, reason=intake.reason,
            strategy_id=strategy_id, tier=TIER_RESEARCH, now=now,
        )
        return ResearchCycleResult(
            intake=intake, report=None, artifact_id=None,
            artifact_passed=None,
        )

    report = evaluate(
        primary_returns=primary_returns,
        cross_section_returns=cross_section_returns,
        sweep_metric=sweep_metric,
        ablation_series=ablation_series,
        walk_forward_folds=walk_forward_folds,
        oos_period_days=oos_period_days,
        trades_per_regime=trades_per_regime,
        n_trials=n_trials, variance_trials=variance_trials,
    )
    metrics = report.to_metrics()
    artifact_id, evaluation = record_validation_artifact(
        conn,
        strategy_id=strategy_id, strategy_ver=strategy_ver,
        tier=TIER_RESEARCH,
        code_hash=code_hash, config_hash=config_hash,
        metrics=metrics,
        validation_policy_lock=validation_policy_lock,
        now=now,
    )
    if not evaluation.pass_:
        record_rejection(
            conn, hypothesis_hash=hypothesis_hash,
            reason=f"tier-1 fail: {'; '.join(evaluation.failure_reasons)}",
            strategy_id=strategy_id, tier=TIER_RESEARCH, now=now,
        )
    return ResearchCycleResult(
        intake=intake, report=report,
        artifact_id=artifact_id, artifact_passed=evaluation.pass_,
    )


__all__ = ["ResearchCycleResult", "run_cycle"]
