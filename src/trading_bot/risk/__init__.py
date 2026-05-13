"""L6 risk kernel — final veto layer.

Plan v4 §6. See ``README.md`` for the mandate. Every intent passes
through ``precheck.evaluate`` before reaching ``execution/``. Decisions
are logged via ``ledger.write_decision``.
"""
from __future__ import annotations

from trading_bot.risk import precheck  # noqa: F401
from trading_bot.risk.kill_switches import (
    KILL_TYPES, Kill, active_kills, clear as clear_kill,
    ensure_kill_switch_table, fire as fire_kill,
)
from trading_bot.risk.limits import RiskLimits, parse_risk_policy
from trading_bot.risk.policy_loader import (
    DEFAULT_HASHES_PATH, DEFAULT_POLICY_DIR, LOCK_FILES,
    PolicyBundle, PolicyHashMismatch, honor_cooldown, load_policy,
    verify_policy_hashes,
)
from trading_bot.risk.types import AccountState, Position, RiskDecision

__all__ = [
    "AccountState",
    "DEFAULT_HASHES_PATH",
    "DEFAULT_POLICY_DIR",
    "Kill",
    "KILL_TYPES",
    "LOCK_FILES",
    "PolicyBundle",
    "PolicyHashMismatch",
    "Position",
    "RiskDecision",
    "RiskLimits",
    "active_kills",
    "clear_kill",
    "ensure_kill_switch_table",
    "fire_kill",
    "honor_cooldown",
    "load_policy",
    "parse_risk_policy",
    "precheck",
    "verify_policy_hashes",
]
