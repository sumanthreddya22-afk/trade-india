"""Feature flags for v4 Phase 0+.

Single source of truth for kernel-vs-research path gating. v4 Section 1A
says LLM is only allowed in L3 (research) and L8 (postmortem); every
decision-path debate (entry / hold / scout / wheel for stocks, crypto,
options) must early-exit unless the operator explicitly opts in.

Env var:
    TRADING_BOT_ENABLE_LLM_HOTPATH = "true" | "1" -> enable; anything
    else (including unset) keeps the hot path disabled.

Why a global module not a config row: a feature flag that lives in a
database can be silently mutated by another process; an env var requires
explicit operator intent at daemon startup, which is exactly the
provenance v4 Section 4 demands.

See docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-0-design.md.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

_ENV_VAR = "TRADING_BOT_ENABLE_LLM_HOTPATH"
_TRUTHY = {"1", "true", "yes", "on"}


def is_llm_hotpath_enabled() -> bool:
    """True iff TRADING_BOT_ENABLE_LLM_HOTPATH is explicitly truthy.

    Read on every call (not cached) so a test or CLI tool can set the env
    var mid-process and have the change take effect immediately.
    """
    raw = os.environ.get(_ENV_VAR, "")
    return raw.strip().lower() in _TRUTHY


VerdictT = Literal["skip", "open", "block", "abstain"]


@dataclass(frozen=True)
class SkippedDebate:
    """Sentinel returned by every quarantined debate entry point.

    Callers that switch on ``verdict`` already handle non-``open``
    verdicts, so swapping in this sentinel is a non-breaking change.
    """

    reason: str = "hotpath_disabled"
    verdict: VerdictT = "skip"
    # Optional context fields populated by callers. Keep empty by default
    # so the sentinel is cheap to construct.
    symbol: str = ""
    pipeline: str = ""

    def __bool__(self) -> bool:
        # Explicit: a skipped debate is falsy so legacy code like
        # `if debate_result:` treats it as "no action".
        return False


def log_quarantine(pipeline: str, symbol: str = "") -> None:
    """One-line info log so the operator sees the skip in the daemon log.

    Idempotent in spirit — callers may invoke once per skipped debate;
    this function does not deduplicate. Email/dashboard dedup lives at a
    higher layer (commit 94a819e centralized per-kind dedup).
    """
    detail = f" symbol={symbol}" if symbol else ""
    log.info("v4 Phase 0: LLM hot-path disabled, skipping %s debate%s", pipeline, detail)


_LIVE_WRITES_ENV = "TRADING_BOT_ALLOW_LIVE_PARAM_WRITES"


def live_param_writes_allowed() -> bool:
    """True iff TRADING_BOT_ALLOW_LIVE_PARAM_WRITES is explicitly truthy.

    v4 Section 4 forbids un-signed live parameter mutation. Phase 0 closes
    the four auto-tune live-write paths (``write_override`` shadow=False,
    ``evolution.save_params``). Operators who need to hand-promote a value
    can set the env var, document the change, and re-run — making it an
    explicit, logged action. Same shape as ``is_llm_hotpath_enabled``.
    """
    raw = os.environ.get(_LIVE_WRITES_ENV, "")
    return raw.strip().lower() in _TRUTHY


def log_live_write_blocked(call_site: str, detail: str = "") -> None:
    """One-line warning logged the first time a blocked write fires.

    Caller-supplied call_site identifies which path attempted to write
    (e.g. "threshold_overrides.write_override" or "evolution.save_params").
    """
    suffix = f" — {detail}" if detail else ""
    log.warning(
        "v4 Phase 0: live param write blocked at %s%s "
        "(set TRADING_BOT_ALLOW_LIVE_PARAM_WRITES=1 to re-enable; "
        "see docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-0-design.md)",
        call_site, suffix,
    )


__all__ = [
    "SkippedDebate",
    "is_llm_hotpath_enabled",
    "live_param_writes_allowed",
    "log_live_write_blocked",
    "log_quarantine",
]
