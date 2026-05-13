"""L3 research factory.

Plan v4 §3 + §8 + §13. Phase 5 ships DSR, PBO, walk-forward + locked
holdout, ablation, parameter plateau, failure memory (90-day reject
cache), adversarial-pair hypothesis intake (mock persona shim), and
the run_cycle driver that ties the four sub-modules together.
"""
from __future__ import annotations

from trading_bot.research.ablation import (
    AblationResult, is_monotone_degradation,
)
from trading_bot.research.dsr import (
    DSRResult, deflated_sharpe, sharpe_ratio,
)
from trading_bot.research.failure_memory import (
    DEFAULT_TTL_DAYS, is_blocked, record_rejection,
)
from trading_bot.research.hypothesis_intake import (
    HypothesisProposal, IntakeResult, MockPersonaRunner,
    PersonaRunnerT, run_intake,
)
from trading_bot.research.parameter_plateau import (
    PlateauResult, plateau_coverage,
)
from trading_bot.research.pbo import PBOResult, probability_of_overfit
from trading_bot.research.persona_schema import (
    ALLOWED_SUBJECT_KINDS, ALLOWED_VERDICTS,
    PersonaOutputError, validate_persona_output,
)
from trading_bot.research.robustness_lab import (
    RobustnessReport, evaluate,
)
from trading_bot.research.run_research import (
    ResearchCycleResult, run_cycle,
)
from trading_bot.research.walkforward import (
    Fold, WalkforwardSchedule, build_folds,
)

# Phase 6
from trading_bot.research.bh_fdr import (
    BHFDRReport, BHFDRRow, adjust as bh_fdr_adjust, apply as bh_fdr_apply,
)
from trading_bot.research.mutation_engine import (
    Candidate, DEFAULT_BUDGET_PER_FAMILY,
    list_candidates, propose_candidates,
    record_candidate, record_outcome,
)
from trading_bot.research.mutation_schema import ensure_mutation_tables
from trading_bot.research.persona_runner import (
    PersonaHashMismatch, PersonaInvocationError,
    SubprocessPersonaRunner, verify_persona_hash,
)
from trading_bot.research import sandbox  # noqa: F401
from trading_bot.research.run_mutation_cycle import (
    BacktestT, MutationCycleReport, run_cycle as run_mutation_cycle,
)

__all__ = [
    "ALLOWED_SUBJECT_KINDS",
    "ALLOWED_VERDICTS",
    "AblationResult",
    "DEFAULT_TTL_DAYS",
    "DSRResult",
    "Fold",
    "HypothesisProposal",
    "IntakeResult",
    "MockPersonaRunner",
    "PBOResult",
    "PersonaOutputError",
    "PersonaRunnerT",
    "PlateauResult",
    "ResearchCycleResult",
    "RobustnessReport",
    "WalkforwardSchedule",
    "build_folds",
    "deflated_sharpe",
    "evaluate",
    "is_blocked",
    "is_monotone_degradation",
    "plateau_coverage",
    "probability_of_overfit",
    "record_rejection",
    "run_cycle",
    "run_intake",
    "sharpe_ratio",
    "validate_persona_output",
    # Phase 6
    "BHFDRReport",
    "BHFDRRow",
    "BacktestT",
    "Candidate",
    "DEFAULT_BUDGET_PER_FAMILY",
    "MutationCycleReport",
    "PersonaHashMismatch",
    "PersonaInvocationError",
    "SubprocessPersonaRunner",
    "bh_fdr_adjust",
    "bh_fdr_apply",
    "ensure_mutation_tables",
    "list_candidates",
    "propose_candidates",
    "record_candidate",
    "record_outcome",
    "run_mutation_cycle",
    "sandbox",
    "verify_persona_hash",
]
