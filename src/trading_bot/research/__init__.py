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
]
