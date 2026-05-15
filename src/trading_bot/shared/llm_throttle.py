"""Priority-queue + daily-budget throttle for Claude CLI calls.

Plan v4 Phase 12 / Phase 0 of the autonomy expansion:

- All persona invocations route through this throttle so we stay under
  the Max-5x daily cap (~240 calls/day; default soft cap 180 with 25 %
  headroom).
- Priority tiers determine when calls get deferred or dropped:
    P0  — never throttled (regime alerts, kill-switch postmortems)
    P1  — defer when budget used >= 80 % (drift postmortems, manual)
    P2  — defer when budget used >= 60 % (mutation review, codegen)
    P3  — drop  when budget used >= 40 % (scout summaries, expansions)
- Content-hash dedup: an identical (persona_id, model, prompt) within
  the TTL returns the cached response without spending budget.

The throttle is intentionally minimal — it does not implement a real
scheduler. ``acquire()`` decides drop/defer/proceed *right now*; the
caller is expected to retry deferred calls on a later tick (the daemon
job loop already does that naturally for batched jobs).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from trading_bot.ledger.llm_call_event import calls_today

log = logging.getLogger(__name__)

Priority = Literal["P0", "P1", "P2", "P3"]
Verdict = Literal["proceed", "defer", "drop", "cache_hit"]

DEFAULT_DAILY_CAP = 180
"""Default soft cap on non-cache-hit calls per UTC day. The Max-5x tier
ceiling is around 240/day; 180 leaves 25 % headroom for unanticipated
bursts (e.g. crisis day with hourly regime analyst memos)."""

_PRIORITY_BLOCK_PCT: dict[Priority, float] = {
    "P0": 1.10,   # never blocks
    "P1": 0.80,
    "P2": 0.60,
    "P3": 0.40,
}

_PRIORITY_DROP_PCT: dict[Priority, float] = {
    "P0": 1.10,
    "P1": 1.10,
    "P2": 1.10,
    "P3": 0.40,   # P3 calls are dropped once the soft cap is breached
}


_TIER_CAP = {
    "max20x": 960,
    "max5x": 240,
    "pro": 60,
}


def daily_cap() -> int:
    """Resolve the daily call cap from env, with sensible fallback."""
    raw = os.environ.get("TRADING_BOT_LLM_DAILY_BUDGET_OVERRIDE")
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except ValueError:
            log.warning("invalid TRADING_BOT_LLM_DAILY_BUDGET_OVERRIDE=%r; "
                        "falling back to tier default", raw)
    tier = os.environ.get("TRADING_BOT_CLAUDE_TIER", "max5x").strip().lower()
    ceiling = _TIER_CAP.get(tier, _TIER_CAP["max5x"])
    # 25 % headroom under the tier ceiling.
    return min(DEFAULT_DAILY_CAP, max(1, int(ceiling * 0.75)))


def cache_dir() -> Path:
    raw = os.environ.get("TRADING_BOT_LLM_CACHE_DIR")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "trading_bot" / "llm"


def input_hash(persona_id: str, model: str, prompt: str) -> str:
    """Content-addressed key for cache lookup + ledger row."""
    h = hashlib.sha256()
    h.update(persona_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    return h.hexdigest()


# ---- Per-persona TTLs (seconds) -------------------------------------------
# Regime alerts must stay fresh; scout summaries can be cached for a day;
# codegen never expires (the same blueprint always produces the same code).
DEFAULT_TTL = 3600
_PERSONA_TTL: dict[str, int] = {
    "regime_analyst": 3600,                # 1 h
    "drift_postmortem": 6 * 3600,          # 6 h (event-specific anyway)
    "universe_audit_analyst": 7 * 24 * 3600,
    "scout_summarizer": 24 * 3600,         # 1 d
    "mutation_reviewer": 7 * 24 * 3600,
    "mutation_proposer": 12 * 3600,
    "strategy_scout": 12 * 3600,
    "search_space_expander": 30 * 24 * 3600,
    "strategy_implementer": 365 * 24 * 3600,
    "quant_research_lead": 24 * 3600,
    "risk_validator": 24 * 3600,
}


def ttl_for(persona_id: str) -> int:
    return _PERSONA_TTL.get(persona_id, DEFAULT_TTL)


@dataclass
class CacheEntry:
    payload: str
    written_at: float = field(default_factory=time.time)


def _cache_path(persona_id: str, key: str) -> Path:
    safe = persona_id.replace("/", "_")
    return cache_dir() / safe / f"{key}.json"


def cache_get(persona_id: str, key: str) -> Optional[str]:
    p = _cache_path(persona_id, key)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text())
    except json.JSONDecodeError:
        log.warning("corrupt llm cache entry %s; ignoring", p)
        return None
    ttl = ttl_for(persona_id)
    age = time.time() - float(obj.get("written_at", 0))
    if age > ttl:
        return None
    return obj.get("payload")


def cache_put(persona_id: str, key: str, payload: str) -> None:
    p = _cache_path(persona_id, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "payload": payload,
        "written_at": time.time(),
    }))


@dataclass(frozen=True)
class ThrottleDecision:
    verdict: Verdict
    used_today: int
    cap: int
    reason: str = ""


def acquire(
    *,
    persona_id: str,
    priority: Priority,
    conn: Optional[sqlite3.Connection],
) -> ThrottleDecision:
    """Decide whether the caller may proceed *now*.

    The decision is local — there is no global mutex. Two concurrent
    callers could each see ``used_today < cap`` and both proceed; in
    practice the daemon only runs one job at a time so this is fine,
    and a small drift over the soft cap is harmless.
    """
    cap = daily_cap()
    used = calls_today(conn) if conn is not None else 0

    # P0 is unconditional — regime alerts, kill-switch postmortems.
    if priority == "P0":
        return ThrottleDecision(
            verdict="proceed", used_today=used, cap=cap, reason="P0 unconditional",
        )

    used_pct = used / max(1, cap)
    block_pct = _PRIORITY_BLOCK_PCT.get(priority, 1.10)
    drop_pct = _PRIORITY_DROP_PCT.get(priority, 1.10)

    if used_pct >= drop_pct and priority == "P3":
        return ThrottleDecision(
            verdict="drop", used_today=used, cap=cap,
            reason=f"P3 dropped at {used}/{cap} (>= {drop_pct:.0%})",
        )
    if used_pct >= block_pct:
        return ThrottleDecision(
            verdict="defer", used_today=used, cap=cap,
            reason=f"{priority} deferred at {used}/{cap} (>= {block_pct:.0%})",
        )
    return ThrottleDecision(
        verdict="proceed", used_today=used, cap=cap, reason="ok",
    )


__all__ = [
    "DEFAULT_DAILY_CAP",
    "Priority",
    "ThrottleDecision",
    "Verdict",
    "acquire",
    "cache_dir",
    "cache_get",
    "cache_put",
    "daily_cap",
    "input_hash",
    "ttl_for",
]
