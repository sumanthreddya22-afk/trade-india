"""MailboxBackedClient — drop-in replacement for AnthropicClient that
prefers the Claude Code subscription via llm_mailbox, falling back to
the direct API call on timeout / mailbox failure / disabled-by-flag.

Same .complete() / .complete_structured() signatures as AnthropicClient,
so callers don't need to know which transport was used.

Activation: opt-in via env or constructor arg. Defaults to disabled so
this lands as zero-impact infrastructure until you opt a role in.

Recommended pilot: decision_reflector (nightly batch, latency-tolerant,
low-stakes). Bad fit: risk_debate (must answer in <60s during scanner
run; mailbox timeout would just add latency before the same API call).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

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


class MailboxBackedClient:
    """Mirrors AnthropicClient surface: .complete() and .complete_structured().

    Each method first submits a brief to the mailbox and waits up to
    `routing.timeout_seconds`. On timeout the call falls through to the
    underlying AnthropicClient — guaranteeing the role still gets an
    answer if the routine is offline.
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
        self._inner = AnthropicClient(
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
