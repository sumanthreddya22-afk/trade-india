"""L4 strategy registry.

Plan v4 §3 + §4 + §13. Phase 4 ships the registry tables, validation
artifact + tier-threshold evaluation, promotion gate (Tier-1 / 2 / 3),
and the hash-locked search space (Plan §8).
"""
from __future__ import annotations

from trading_bot.registry.promotion import (
    PromotionDecision, compute_packet_id, gate, record_promotion_packet,
)
from trading_bot.registry.schema import (
    REGISTRY_DDL, ensure_registry_tables,
)
from trading_bot.registry.search_space import (
    DEFAULT_PATH as SEARCH_SPACE_DEFAULT_PATH,
    SearchSpace, SearchSpaceError,
    get_dimension, list_dimensions, load_search_space, validate_mutation_id,
)
from trading_bot.registry.strategies import (
    ACTIVE_TRADING_STATUSES, EXIT_ONLY_STATUSES, NON_ACTIVE_STATUSES,
    RESEARCH_ONLY, StrategyVersion, VersionNotFound,
    get_active_version, list_versions, register_version,
)
from trading_bot.registry.validation_artifacts import (
    GATE_LENS, TIERS, TIER_LIVE, TIER_PAPER, TIER_RESEARCH,
    TierEvaluation, compute_artifact_id, evaluate_tier,
    find_latest_pass, record_validation_artifact,
)

__all__ = [
    "ACTIVE_TRADING_STATUSES",
    "EXIT_ONLY_STATUSES",
    "GATE_LENS",
    "NON_ACTIVE_STATUSES",
    "PromotionDecision",
    "REGISTRY_DDL",
    "RESEARCH_ONLY",
    "SEARCH_SPACE_DEFAULT_PATH",
    "SearchSpace",
    "SearchSpaceError",
    "StrategyVersion",
    "TIERS",
    "TIER_LIVE",
    "TIER_PAPER",
    "TIER_RESEARCH",
    "TierEvaluation",
    "VersionNotFound",
    "compute_artifact_id",
    "compute_packet_id",
    "ensure_registry_tables",
    "evaluate_tier",
    "find_latest_pass",
    "gate",
    "get_active_version",
    "get_dimension",
    "list_dimensions",
    "list_versions",
    "load_search_space",
    "record_promotion_packet",
    "record_validation_artifact",
    "register_version",
    "validate_mutation_id",
]
