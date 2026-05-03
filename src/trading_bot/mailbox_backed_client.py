"""MailboxBackedClient — drop-in replacement for AnthropicClient that
prefers the Claude Code subscription, falling back gracefully when no
upstream is available.

Three transport tiers (highest preference first):
  1. ``ClaudeCliTransport`` (Max 5x subscription via ``claude`` CLI
     subprocess) — the canonical production path on this machine.
     Used when ANTHROPIC_API_KEY is NOT set, OR when the operator
     sets ``TRADING_BOT_LLM_PREFER_CLI=1`` to force the CLI even with
     creds present.
  2. ``MailboxQueue`` (file-backed routine bridge) — opt-in via
     ``TRADING_BOT_MAILBOX_ENABLED=1``. Original Phase 6 path.
  3. Direct ``AnthropicClient`` (Anthropic API) — fallback when
     creds ARE set and the operator hasn't opted into either of the
     above.

Same .complete() / .complete_structured() signatures as AnthropicClient,
so callers don't need to know which transport was used. The CLI path
emulates the AnthropicClient's tool-call structured response shape by
running ``ClaudeCliTransport.complete_structured`` with a JSON schema
derived from the legacy ``tool_schema`` arg, then wrapping the parsed
JSON dict back into a ``StructuredResponse``.

Why this matters for stocks: the legacy stocks scout / hold / entry /
unblock debates were written before the CLI transport existed and
called this client directly. Without ANTHROPIC_API_KEY set on the
machine they were skipping silently every tick. Now they ride the
subscription quota the same way the crypto + options pipelines do.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from trading_bot.anthropic_client import (
    AnthropicClient,
    AnthropicCredsMissingError,
    AnthropicResponse,
    BudgetExceededError,
    StructuredResponse,
)
from trading_bot.llm_mailbox import Brief, MailboxQueue


log = logging.getLogger(__name__)

_MAILBOX_ENABLED_ENV = "TRADING_BOT_MAILBOX_ENABLED"
_MAILBOX_TIMEOUT_ENV = "TRADING_BOT_MAILBOX_TIMEOUT_SECONDS"
# Operator-stated plan: every LLM call rides the Claude Max 5x
# subscription via the CLI subprocess transport. The Anthropic API
# path is opt-in only — operator sets this env var (useful for A/B
# comparison with a provisioned API key).
_PREFER_API_ENV = "TRADING_BOT_LLM_PREFER_API"


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip()) if os.environ.get(name) else default
    except ValueError:
        return default


@dataclass
class MailboxRouting:
    enabled: bool
    timeout_seconds: float
    model_class: str    # "judge" | "debater" | "reflector" | "architect"


def _wants_api_transport() -> bool:
    """Opt-in to use the Anthropic API path. Default is CLI subscription."""
    return _env_bool(_PREFER_API_ENV, default=False)


class _CliFallbackClient:
    """Inner-client adapter that routes legacy ``MailboxBackedClient``
    calls through ``shared.llm_transport.ClaudeCliTransport`` (Max 5x
    subscription path) and translates response shapes back to the
    legacy ``AnthropicResponse`` / ``StructuredResponse`` types so
    every legacy caller stays unchanged.

    Translation contract for ``complete_structured``:
      legacy call → tool_name + tool_schema (Anthropic tool-call shape)
      we discard tool_name/description; pass tool_schema directly as
      json_schema to ClaudeCliTransport.complete_structured. The CLI's
      ``--json-schema`` flag accepts the same JSON schema dialect, so
      the schema doesn't need rewriting.
      response.text is the JSON string; we parse it back into a dict
      and stamp ``used_structured=True`` to match legacy contract.
    """

    def __init__(self, *, role_name: str, engine):
        from trading_bot.shared.llm_transport import get_transport
        self._role_name = role_name
        self._transport = get_transport(role_name=role_name, engine=engine)

    def complete(
        self, *, system: str, messages: list[dict], max_tokens: int = 4096,
    ) -> AnthropicResponse:
        out = self._transport.complete(
            system=system, messages=messages, max_tokens=max_tokens,
        )
        return AnthropicResponse(
            text=out.text,
            input_tokens=out.input_tokens,
            output_tokens=out.output_tokens,
            request_id=out.request_id,
            model=out.model,
        )

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[dict],
        tool_name: str,
        tool_description: str,
        tool_schema: dict,
        max_tokens: int = 4096,
    ) -> StructuredResponse:
        out = self._transport.complete_structured(
            system=system, messages=messages,
            json_schema=tool_schema,
            max_tokens=max_tokens,
        )
        # CLI returns JSON in .text; some CLI versions also stash the
        # parsed object under raw['result']. Try both.
        parsed: Optional[dict] = None
        if isinstance(out.raw, dict):
            cand = out.raw.get("result")
            if isinstance(cand, dict):
                parsed = cand
        if parsed is None:
            text = (out.text or "").strip()
            if text:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
        return StructuredResponse(
            data=parsed,
            text=out.text,
            used_structured=parsed is not None,
            input_tokens=out.input_tokens,
            output_tokens=out.output_tokens,
            request_id=out.request_id,
            model=out.model,
        )


def _build_inner_client(*, role_name: str, model: str, engine):
    """Construct the inner client this MailboxBackedClient wraps.

    Operator-stated default: every LLM call rides the Claude Max 5x
    subscription via the CLI subprocess. The Anthropic API path is
    opt-in only — set ``TRADING_BOT_LLM_PREFER_API=1`` to use it (e.g.
    for A/B comparison when an API key is provisioned).

    Order of preference:
      1. AnthropicClient (direct API) — only when TRADING_BOT_LLM_PREFER_API=1
         AND ANTHROPIC_API_KEY is available. Falls back to CLI on creds
         missing.
      2. ClaudeCliTransport (subscription) — the default for every role.
    """
    if _wants_api_transport():
        try:
            return AnthropicClient(role_name=role_name, model=model, engine=engine)
        except AnthropicCredsMissingError:
            log.info(
                "mailbox_backed_client: PREFER_API=1 but no key — falling "
                "back to ClaudeCliTransport for role=%s", role_name,
            )
    return _CliFallbackClient(role_name=role_name, engine=engine)


class MailboxBackedClient:
    """Mirrors AnthropicClient surface: .complete() and .complete_structured().

    Each method first submits a brief to the mailbox and waits up to
    `routing.timeout_seconds`. On timeout the call falls through to the
    underlying inner client — guaranteeing the role still gets an
    answer if the routine is offline.

    The inner client is now selected at construction time:
      - CLI transport (subscription) when no API key set OR when forced
        via TRADING_BOT_LLM_PREFER_CLI=1.
      - AnthropicClient (direct API) otherwise.
    """

    def __init__(
        self,
        *,
        role_name: str,
        model: str,
        engine,
        routing: MailboxRouting | None = None,
        mailbox: MailboxQueue | None = None,
    ):
        self._inner = _build_inner_client(
            role_name=role_name, model=model, engine=engine,
        )
        self._role_name = role_name
        self._routing = routing or MailboxRouting(
            enabled=_env_bool(_MAILBOX_ENABLED_ENV, default=False),
            timeout_seconds=_env_float(_MAILBOX_TIMEOUT_ENV, default=900.0),
            model_class="reflector",
        )
        self._mailbox = mailbox or MailboxQueue()

    def complete(
        self, *, system: str, messages: list[dict], max_tokens: int = 4096,
    ) -> AnthropicResponse:
        if not self._routing.enabled:
            return self._inner.complete(
                system=system, messages=messages, max_tokens=max_tokens,
            )

        brief = Brief(
            role=self._role_name,
            model_class=self._routing.model_class,
            system=system, messages=messages, max_tokens=max_tokens,
            deadline_seconds=int(self._routing.timeout_seconds),
        )
        try:
            result = self._mailbox.submit_and_wait(
                brief, timeout_seconds=self._routing.timeout_seconds,
            )
        except Exception as e:
            log.warning("mailbox submit failed (%s) — falling back to API", e)
            result = None

        if result is None or result.error:
            if result and result.error:
                log.info("mailbox returned error '%s' — falling back to API",
                         result.error)
            return self._inner.complete(
                system=system, messages=messages, max_tokens=max_tokens,
            )

        return AnthropicResponse(
            text=result.text,
            input_tokens=result.input_tokens or 0,
            output_tokens=result.output_tokens or 0,
            request_id=result.id,
            model=result.model_used or "mailbox",
        )

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[dict],
        tool_name: str,
        tool_description: str,
        tool_schema: dict,
        max_tokens: int = 4096,
    ) -> StructuredResponse:
        if not self._routing.enabled:
            return self._inner.complete_structured(
                system=system, messages=messages, tool_name=tool_name,
                tool_description=tool_description, tool_schema=tool_schema,
                max_tokens=max_tokens,
            )

        brief = Brief(
            role=self._role_name,
            model_class=self._routing.model_class,
            system=system, messages=messages, max_tokens=max_tokens,
            tool_name=tool_name, tool_description=tool_description,
            tool_schema=tool_schema,
            deadline_seconds=int(self._routing.timeout_seconds),
        )
        try:
            result = self._mailbox.submit_and_wait(
                brief, timeout_seconds=self._routing.timeout_seconds,
            )
        except Exception as e:
            log.warning("mailbox submit failed (%s) — falling back to API", e)
            result = None

        if result is None or result.error:
            if result and result.error:
                log.info("mailbox returned error '%s' — falling back to API",
                         result.error)
            return self._inner.complete_structured(
                system=system, messages=messages, tool_name=tool_name,
                tool_description=tool_description, tool_schema=tool_schema,
                max_tokens=max_tokens,
            )

        return StructuredResponse(
            data=result.structured,
            text=result.text,
            used_structured=result.structured is not None,
            input_tokens=result.input_tokens or 0,
            output_tokens=result.output_tokens or 0,
            request_id=result.id,
            model=result.model_used or "mailbox",
        )


__all__ = [
    "MailboxBackedClient",
    "MailboxRouting",
    "AnthropicCredsMissingError",
    "BudgetExceededError",
]
