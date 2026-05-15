"""Single transport for all LLM calls — wraps the ``claude --json`` CLI.

Plan v4 Phase 12 / autonomy-expansion Phase 0:

- Every persona invocation in the codebase routes through ``invoke()``.
- We no longer use the ``anthropic`` SDK directly. The Claude CLI uses
  the operator's Max-subscription seat — no separate API key needed.
- ROLE_MODEL maps personas to ``--model`` flags (``sonnet`` default,
  ``opus`` for codegen + adversarial depth).
- Caching, daily-budget throttling, and the ``llm_call_event`` ledger
  row are wired in transparently.
- Boot check verifies the CLI binary exists (see ``kernel.boot_llm``).

This module is deliberately small — the persona-specific contract
(prompt composition, hash verification, schema validation) lives in
``research.persona_runner``. ``invoke()`` is the low-level transport
that ``persona_runner.SubprocessPersonaRunner`` plus ad-hoc callers
(regime analyst, drift postmortem, etc.) share.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from trading_bot.shared.llm_throttle import (
    Priority,
    ThrottleDecision,
    acquire,
    cache_get,
    cache_put,
    input_hash,
)

log = logging.getLogger(__name__)


# ---- Role -> model mapping ------------------------------------------------
# Sonnet is the default. Opus is reserved for codegen + adversarial depth
# (matches CLAUDE.md memory ``feedback_opus_only.md``: Opus judges,
# Sonnet reviewers — but here judges == implementer + adversaries).
ROLE_MODEL: dict[str, str] = {
    # Reviewer / observer roles -> Sonnet
    "regime_analyst": "sonnet",
    "drift_postmortem": "sonnet",
    "universe_audit_analyst": "sonnet",
    "scout_summarizer": "sonnet",
    "mutation_reviewer": "sonnet",
    "strategy_scout": "sonnet",
    "search_space_expander": "sonnet",
    "mutation_proposer": "sonnet",
    "source_proposer": "sonnet",
    # Adversarial debate + codegen -> Opus (judges + implementers)
    "strategy_implementer": "opus",
    "quant_research_lead": "opus",
    "risk_validator": "opus",
}

# ---- Default priority per persona -----------------------------------------
DEFAULT_PRIORITY: dict[str, Priority] = {
    # P0 — never throttled
    "regime_analyst": "P0",
    # P1 — defer at 80 %
    "drift_postmortem": "P1",
    "risk_validator": "P1",
    # P2 — defer at 60 %
    "quant_research_lead": "P2",
    "mutation_proposer": "P2",
    "mutation_reviewer": "P2",
    "strategy_implementer": "P2",
    "universe_audit_analyst": "P2",
    # P3 — drop at 40 %
    "scout_summarizer": "P3",
    "strategy_scout": "P3",
    "search_space_expander": "P3",
    "source_proposer": "P3",
}


CLAUDE_CLI_DEFAULT = "claude"


def claude_cli_path() -> str:
    raw = os.environ.get("TRADING_BOT_CLAUDE_CLI_PATH")
    if raw and raw.strip():
        return raw.strip()
    return CLAUDE_CLI_DEFAULT


def claude_cli_available() -> bool:
    """Return True iff the configured claude CLI binary is on PATH."""
    return shutil.which(claude_cli_path()) is not None


def resolve_model(role: str, override: Optional[str] = None) -> str:
    if override:
        return override
    return ROLE_MODEL.get(role, "sonnet")


def resolve_priority(role: str, override: Optional[Priority] = None) -> Priority:
    if override is not None:
        return override
    return DEFAULT_PRIORITY.get(role, "P2")


class LLMUnavailable(RuntimeError):
    """Raised when the throttle drops or defers a call the caller insisted on."""


@dataclass
class LLMResponse:
    raw_stdout: str
    text: str
    model: str
    priority: Priority
    input_hash: str
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    latency_ms: int
    deferred: bool
    dropped: bool


def _spawn_claude(prompt: str, model: str, timeout_s: int) -> tuple[str, int]:
    """Run ``claude --output-format json -p`` with the given prompt.

    Returns (raw_stdout, latency_ms). Raises on subprocess error.
    """
    binary = claude_cli_path()
    cmd = [binary, "--output-format", "json", "-p"]
    if model:
        cmd.extend(["--model", model])
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise LLMUnavailable(
            f"claude binary not found at {binary!r}; set TRADING_BOT_CLAUDE_CLI_PATH"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise LLMUnavailable(
            f"claude subprocess timed out after {timeout_s}s"
        ) from e
    latency_ms = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        raise LLMUnavailable(
            f"claude exit={proc.returncode}: {proc.stderr[:200]}"
        )
    return proc.stdout, latency_ms


def _parse_cli_response(stdout: str) -> tuple[str, int, int]:
    """Pull (text, input_tokens, output_tokens) from the CLI JSON envelope.

    The Claude CLI emits a JSON object like:
    {"type":"result","result":"...","usage":{"input_tokens":N,
     "output_tokens":M}, ...}
    If the envelope is missing fields we degrade gracefully.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, 0, 0
    if not isinstance(envelope, dict):
        return stdout, 0, 0
    text = envelope.get("result", "") or ""
    usage = envelope.get("usage") or {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    return str(text), in_tok, out_tok


def invoke(
    *,
    role: str,
    prompt: str,
    conn: Optional[sqlite3.Connection] = None,
    model_override: Optional[str] = None,
    priority_override: Optional[Priority] = None,
    timeout_s: int = 120,
    bypass_cache: bool = False,
    bypass_throttle: bool = False,
) -> LLMResponse:
    """Run a Claude CLI invocation for ``role`` with ``prompt``.

    The call is fully observed: cache hit / miss, throttle outcome, and
    ledger row are all written before returning. ``conn`` must be a
    writer connection (the ledger is append-only). When ``conn`` is
    ``None`` (test seam) ledger writes are skipped.
    """
    model = resolve_model(role, model_override)
    priority = resolve_priority(role, priority_override)
    key = input_hash(role, model, prompt)

    if not bypass_cache:
        cached = cache_get(role, key)
        if cached is not None:
            text, in_tok, out_tok = _parse_cli_response(cached)
            if conn is not None:
                from trading_bot.ledger.llm_call_event import write_event
                write_event(
                    conn,
                    persona_id=role, model=model, priority=priority,
                    input_hash=key, cache_hit=True,
                    input_tokens=in_tok, output_tokens=out_tok,
                    latency_ms=0, deferred=False, dropped=False,
                )
            return LLMResponse(
                raw_stdout=cached, text=text, model=model, priority=priority,
                input_hash=key, cache_hit=True,
                input_tokens=in_tok, output_tokens=out_tok,
                latency_ms=0, deferred=False, dropped=False,
            )

    if not bypass_throttle:
        decision: ThrottleDecision = acquire(
            persona_id=role, priority=priority, conn=conn,
        )
        if decision.verdict == "drop":
            if conn is not None:
                from trading_bot.ledger.llm_call_event import write_event
                write_event(
                    conn,
                    persona_id=role, model=model, priority=priority,
                    input_hash=key, cache_hit=False,
                    latency_ms=0, deferred=False, dropped=True,
                )
            raise LLMUnavailable(decision.reason)
        if decision.verdict == "defer":
            if conn is not None:
                from trading_bot.ledger.llm_call_event import write_event
                write_event(
                    conn,
                    persona_id=role, model=model, priority=priority,
                    input_hash=key, cache_hit=False,
                    latency_ms=0, deferred=True, dropped=False,
                )
            raise LLMUnavailable(decision.reason)

    raw, latency = _spawn_claude(prompt, model=model, timeout_s=timeout_s)
    text, in_tok, out_tok = _parse_cli_response(raw)
    cache_put(role, key, raw)
    if conn is not None:
        from trading_bot.ledger.llm_call_event import write_event
        write_event(
            conn,
            persona_id=role, model=model, priority=priority,
            input_hash=key, cache_hit=False,
            input_tokens=in_tok, output_tokens=out_tok,
            latency_ms=latency, deferred=False, dropped=False,
        )
    return LLMResponse(
        raw_stdout=raw, text=text, model=model, priority=priority,
        input_hash=key, cache_hit=False,
        input_tokens=in_tok, output_tokens=out_tok,
        latency_ms=latency, deferred=False, dropped=False,
    )


__all__ = [
    "CLAUDE_CLI_DEFAULT",
    "DEFAULT_PRIORITY",
    "LLMResponse",
    "LLMUnavailable",
    "ROLE_MODEL",
    "claude_cli_available",
    "claude_cli_path",
    "invoke",
    "resolve_model",
    "resolve_priority",
]
