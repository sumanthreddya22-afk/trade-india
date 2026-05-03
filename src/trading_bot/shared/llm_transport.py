"""LLM transport — Claude CLI subprocess as primary path (Max subscription).

Single point through which every bot LLM call flows:
- Debate calls (scout, entry, hold, unblock × 3 pipelines)
- Bulk classifier calls (sentiment, adversarial flags)
- Lesson loop + threshold tuner
- Cross-pollination agents (drift detector, PR bot, audit lead)
- Daily summary writer

By routing through ``claude -p`` subprocess, every call consumes the
local Max subscription quota rather than per-token API spend (see
docs/adrs/0002-llm-transport-via-claude-cli-subprocess.md).

Two-tier model policy (deterministic, no Haiku):
- Opus 4.7 → judges, outcome analyzer, threshold tuner, quarterly audit
- Sonnet 4.6 → reviewers, classifiers, drift detector, PR bot, summaries

When Max quota window is exhausted, raises ``SubscriptionRateLimited``;
caller (debate runner) catches and emits a SkipVerdict so the bot falls
through to deterministic gates instead of crashing.

This module is API-shape compatible with the legacy
``trading_bot.anthropic_client.AnthropicClient`` so existing callers
can swap with minimal change.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model policy
# ---------------------------------------------------------------------------

OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"

# Roles map to model tier. Keys are the persona/role IDs used at call
# sites; values are the model name passed to ``claude --model``.
ROLE_MODEL: Dict[str, str] = {
    # judges (high-stakes synthesis) — Opus
    "scout_judge": OPUS,
    "entry_judge": OPUS,
    "hold_judge": OPUS,
    "unblock_judge": OPUS,
    "wheel_judge": OPUS,
    "lesson_analyst": OPUS,
    "threshold_tuner": OPUS,
    "audit_lead": OPUS,
    # reviewers — Sonnet
    "scout_skeptic": SONNET,
    "scout_analyst": SONNET,
    "entry_aggressive": SONNET,
    "entry_conservative": SONNET,
    "entry_neutral": SONNET,
    "hold_aggressive": SONNET,
    "hold_conservative": SONNET,
    "hold_neutral": SONNET,
    "unblock_aggressive": SONNET,
    "unblock_conservative": SONNET,
    "unblock_neutral": SONNET,
    "wheel_aggressive": SONNET,
    "wheel_conservative": SONNET,
    "wheel_neutral": SONNET,
    "assignment_handler": SONNET,
    "roll_advisor": SONNET,
    # classifiers + summaries — Sonnet
    "sentiment_classifier": SONNET,
    "adversarial_classifier": SONNET,
    "summary_writer": SONNET,
    # cross-pollination — Sonnet (audit_lead is the exception, above)
    "drift_detector": SONNET,
    "pr_bot": SONNET,
}


def model_for_role(role: str) -> str:
    """Return the model to use for a given role.

    Defaults to Sonnet when the role is unknown — fail-cheap rather than
    fail-expensive. Add new roles to ROLE_MODEL explicitly.
    """
    return ROLE_MODEL.get(role, SONNET)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LlmTransportError(RuntimeError):
    """Generic transport error (subprocess failure, malformed JSON, etc.)."""


class SubscriptionRateLimited(LlmTransportError):
    """Max subscription quota window exhausted.

    Caller should treat this as a "no LLM verdict available" signal and
    fall through to deterministic gates only. The skip window (default
    5h) is recorded in module state; subsequent calls during the window
    will raise this same exception immediately without invoking
    subprocess (cheap fast-fail).
    """


class CliNotAvailable(LlmTransportError):
    """``claude`` CLI is not on PATH."""


# ---------------------------------------------------------------------------
# Rate-limit window state (module-level; thread-safe)
# ---------------------------------------------------------------------------

_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_UNTIL: Optional[datetime] = None
RATE_LIMIT_WINDOW_MINUTES = 300  # 5h Max plan window


def _is_rate_limited_now() -> bool:
    with _RATE_LIMIT_LOCK:
        if _RATE_LIMIT_UNTIL is None:
            return False
        if datetime.now(timezone.utc) < _RATE_LIMIT_UNTIL:
            return True
        return False


def _record_rate_limit(window_minutes: int = RATE_LIMIT_WINDOW_MINUTES) -> None:
    global _RATE_LIMIT_UNTIL
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_UNTIL = datetime.now(timezone.utc) + timedelta(minutes=window_minutes)
    logger.warning(
        "LLM transport entered rate-limit skip mode until %s (UTC)",
        _RATE_LIMIT_UNTIL.isoformat() if _RATE_LIMIT_UNTIL else "<unknown>",
    )


def reset_rate_limit_window() -> None:
    """Test helper / manual override — clear the skip window."""
    global _RATE_LIMIT_UNTIL
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_UNTIL = None


def rate_limit_status() -> Optional[datetime]:
    """Return the UTC datetime the current skip window ends, or None if not active."""
    with _RATE_LIMIT_LOCK:
        return _RATE_LIMIT_UNTIL


# ---------------------------------------------------------------------------
# CLI binary discovery
# ---------------------------------------------------------------------------


_CLI_FALLBACK_LOCATIONS = (
    "/opt/homebrew/bin/claude",          # Apple-silicon Homebrew (current)
    "/usr/local/bin/claude",             # Intel Homebrew
    str(Path.home() / ".npm-global/bin/claude"),
    str(Path.home() / ".local/bin/claude"),
    str(Path.home() / "node_modules/.bin/claude"),
)


def _resolve_cli_path(cli_path: str) -> str:
    """If ``cli_path`` is a bare name not on PATH, look it up in known
    install locations. Returns an absolute path when one is found, else
    the original (so the caller still raises ``CliNotAvailable`` with a
    clear error). This is what makes the transport survive launchd's
    minimal ``/usr/bin:/bin`` PATH.
    """
    if os.path.sep in cli_path:
        return cli_path
    import shutil
    found = shutil.which(cli_path)
    if found:
        return found
    for candidate in _CLI_FALLBACK_LOCATIONS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return cli_path


# ---------------------------------------------------------------------------
# Response dataclass — shape-compatible with anthropic_client.AnthropicResponse
# ---------------------------------------------------------------------------


@dataclass
class LlmResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    request_id: Optional[str] = None
    cli_session_id: Optional[str] = None
    wall_time_ms: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class ClaudeCliTransport:
    """Subprocess transport using ``claude -p`` for Max subscription quota.

    One instance per caller is fine — it is stateless apart from the
    module-level rate-limit window. Pass ``cli_path`` if ``claude`` is
    not on PATH.
    """

    def __init__(
        self,
        *,
        role_name: str,
        cli_path: str = "claude",
        default_max_tokens: int = 4096,
        default_timeout_seconds: int = 180,
        engine: Any = None,
    ) -> None:
        self.role_name = role_name
        self.cli_path = _resolve_cli_path(cli_path)
        self.default_max_tokens = default_max_tokens
        self.default_timeout = default_timeout_seconds
        self.engine = engine

    # ----- public API -------------------------------------------------

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Dict[str, str]],
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> LlmResponse:
        """Run a non-structured Claude completion via subprocess.

        ``messages`` follows the Anthropic shape: ``[{"role": "user",
        "content": "..."}, ...]``. The CLI takes a single ``-p`` prompt,
        so messages are concatenated into a transcript-style prompt.
        """
        if _is_rate_limited_now():
            raise SubscriptionRateLimited(
                "Skip window active until "
                f"{rate_limit_status()} (UTC) — caller should fall through to deterministic gates"
            )

        prompt = _flatten_messages_to_prompt(messages)
        chosen_model = model or model_for_role(self.role_name)
        chosen_max_tokens = max_tokens or self.default_max_tokens
        chosen_timeout = timeout_seconds or self.default_timeout

        return self._invoke_cli(
            prompt=prompt,
            system=system,
            model=chosen_model,
            max_tokens=chosen_max_tokens,
            timeout_seconds=chosen_timeout,
            json_schema=None,
        )

    def complete_structured(
        self,
        *,
        system: str,
        messages: Sequence[Dict[str, str]],
        json_schema: Dict[str, Any],
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> LlmResponse:
        """Run a Claude completion forced into a JSON-schema shape.

        Uses ``claude --json-schema`` so the model output validates
        against ``json_schema``. The ``LlmResponse.text`` will be a JSON
        string callers can ``json.loads()``.
        """
        if _is_rate_limited_now():
            raise SubscriptionRateLimited(
                "Skip window active until "
                f"{rate_limit_status()} (UTC) — caller should fall through to deterministic gates"
            )

        prompt = _flatten_messages_to_prompt(messages)
        chosen_model = model or model_for_role(self.role_name)
        chosen_max_tokens = max_tokens or self.default_max_tokens
        chosen_timeout = timeout_seconds or self.default_timeout

        return self._invoke_cli(
            prompt=prompt,
            system=system,
            model=chosen_model,
            max_tokens=chosen_max_tokens,
            timeout_seconds=chosen_timeout,
            json_schema=json_schema,
        )

    # ----- internals --------------------------------------------------

    def _invoke_cli(
        self,
        *,
        prompt: str,
        system: str,
        model: str,
        max_tokens: int,
        timeout_seconds: int,
        json_schema: Optional[Dict[str, Any]],
    ) -> LlmResponse:
        cmd: List[str] = [
            self.cli_path,
            "-p", prompt,
            "--model", model,
            "--output-format", "json",
            # Lock down the call: no tools, no slash commands, no session
            # persistence. Each debate is a one-shot completion.
            "--tools", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--permission-mode", "default",
            "--system-prompt", system,
        ]
        if json_schema is not None:
            cmd.extend(["--json-schema", json.dumps(json_schema)])

        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as e:
            raise CliNotAvailable(
                f"`{self.cli_path}` not found on PATH — install Claude Code or pass cli_path"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise LlmTransportError(
                f"`claude` subprocess timed out after {timeout_seconds}s for role={self.role_name}"
            ) from e

        wall_ms = int((time.monotonic() - started) * 1000)

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stderr_lower = stderr.lower()
            # Heuristic rate-limit detection — match what the CLI prints.
            if any(
                marker in stderr_lower
                for marker in ("rate limit", "rate-limit", "5-hour", "quota", "usage limit")
            ):
                _record_rate_limit()
                raise SubscriptionRateLimited(
                    f"Max subscription rate-limited; entering 5h skip window. CLI stderr: {stderr[:200]}"
                )
            raise LlmTransportError(
                f"`claude` exited {result.returncode} for role={self.role_name}: {stderr[:400]}"
            )

        stdout = result.stdout or ""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise LlmTransportError(
                f"`claude` returned malformed JSON for role={self.role_name}: {stdout[:400]}"
            ) from e

        text = _extract_text(payload)
        usage = payload.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        request_id = payload.get("request_id") or payload.get("id")
        cli_session_id = payload.get("session_id")

        # Record cost-tracker entry. Subscription calls have $0 marginal
        # cost but we still want call-count visibility for quota
        # awareness. cost_tracker is optional (engine may be None in
        # bare unit tests).
        if self.engine is not None:
            try:
                from trading_bot.cost_tracker import record_call
                from sqlalchemy.orm import Session

                with Session(self.engine) as session:
                    record_call(
                        session,
                        role_name=self.role_name,
                        model=model,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        request_id=request_id,
                    )
            except Exception:
                # Cost tracking should never break a debate. Log + continue.
                logger.exception("cost_tracker.record_call failed for role=%s", self.role_name)

        return LlmResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=model,
            request_id=request_id,
            cli_session_id=cli_session_id,
            wall_time_ms=wall_ms,
            raw=payload if isinstance(payload, dict) else {},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_messages_to_prompt(messages: Sequence[Dict[str, str]]) -> str:
    """Flatten an Anthropic-style messages list into a single prompt string.

    The CLI takes one prompt; we adopt a transcript-like format so any
    multi-turn shape from existing call sites is preserved verbatim.
    Single-message callers get just the content with no decoration.
    """
    if len(messages) == 1 and messages[0].get("role") == "user":
        return str(messages[0].get("content", ""))

    parts: List[str] = []
    for m in messages:
        role = str(m.get("role", "user")).upper()
        content = str(m.get("content", ""))
        parts.append(f"=== {role} ===\n{content}")
    return "\n\n".join(parts)


def _extract_text(payload: Any) -> str:
    """Pull the assistant text out of a ``claude -p --output-format json`` payload.

    The CLI's JSON shape has evolved across versions; we accept several
    common variants and concatenate any text segments we find.
    """
    if not isinstance(payload, dict):
        return str(payload)

    # Most common shape: {"result": "..."}
    if isinstance(payload.get("result"), str):
        return payload["result"]

    # Alternative: {"content": "..."} or {"content": [{"type": "text", "text": "..."}]}
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if isinstance(blk.get("text"), str):
                    chunks.append(blk["text"])
                elif blk.get("type") == "text" and isinstance(blk.get("text"), str):
                    chunks.append(blk["text"])
        if chunks:
            return "".join(chunks)

    # Fall back to ``messages`` if present.
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "assistant":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "".join(b.get("text", "") for b in c if isinstance(b, dict))

    # Give up gracefully — return the whole payload as a string so the
    # caller can debug.
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Convenience: a singleton for callers that don't want to manage instances
# ---------------------------------------------------------------------------

_DEFAULT_TRANSPORT: Optional[ClaudeCliTransport] = None


def get_transport(role_name: str, *, engine: Any = None) -> ClaudeCliTransport:
    """Return a transport for a given role.

    Always returns a fresh instance — instances are cheap (no connection
    pooling). The role_name controls model routing and cost-tracker
    attribution.
    """
    cli_path = os.environ.get("CLAUDE_CLI", "claude")
    return ClaudeCliTransport(role_name=role_name, cli_path=cli_path, engine=engine)
