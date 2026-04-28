"""Anthropic SDK wrapper with retry + cost tracking + cap enforcement.

Wraps `anthropic.Anthropic` so every Claude call records its tokens and
USD cost into state.db. Refuses to call when CostHalt is active.

Defaults:
  Strategy Architect → claude-opus-4-7 (best reasoning for code generation)
  Tone Analyst       → claude-haiku-4-5 (fast/cheap)

Override via env: ANTHROPIC_ARCHITECT_MODEL, ANTHROPIC_TONE_MODEL.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from trading_bot.cost_tracker import is_halted, record_call

# Lab is launchd-managed and doesn't inherit shell env, so explicitly load
# .env at module-import time. Idempotent — load_dotenv won't override values
# already set in the process env (so the plist still wins if set there).
load_dotenv()


class BudgetExceededError(RuntimeError):
    pass


class AnthropicCredsMissingError(RuntimeError):
    pass


@dataclass
class AnthropicResponse:
    text: str
    input_tokens: int
    output_tokens: int
    request_id: str | None
    model: str


def default_architect_model() -> str:
    return os.environ.get("ANTHROPIC_ARCHITECT_MODEL", "claude-opus-4-7")


def default_tone_model() -> str:
    return os.environ.get("ANTHROPIC_TONE_MODEL", "claude-haiku-4-5-20251001")


class AnthropicClient:
    """Thin wrapper around `anthropic.Anthropic.messages.create`."""

    MAX_RETRIES = 3

    def __init__(self, *, role_name: str, model: str, engine):
        self.role_name = role_name
        self.model = model
        self.engine = engine
        self._client = None

    def _get_client(self):
        if self._client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise AnthropicCredsMissingError("ANTHROPIC_API_KEY not set")
            from anthropic import Anthropic

            self._client = Anthropic(api_key=api_key)
        return self._client

    def complete(
        self, *, system: str, messages: list[dict], max_tokens: int = 4096
    ) -> AnthropicResponse:
        # Halt check before spend.
        with Session(self.engine) as session:
            if is_halted(session):
                raise BudgetExceededError(
                    "Anthropic monthly cap exceeded — LLM call refused"
                )

        client = self._get_client()
        last_exc = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = client.messages.create(
                    model=self.model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                # Extract text from content blocks
                text_parts = []
                for block in resp.content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                text = "".join(text_parts)
                in_tokens = resp.usage.input_tokens
                out_tokens = resp.usage.output_tokens
                request_id = getattr(resp, "id", None)
                # Record cost.
                with Session(self.engine) as session:
                    record_call(
                        session,
                        role_name=self.role_name,
                        model=self.model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        request_id=request_id,
                    )
                return AnthropicResponse(
                    text=text,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    request_id=request_id,
                    model=self.model,
                )
            except Exception as e:
                last_exc = e
                # Retry only on rate-limit / 5xx style errors. Anthropic SDK
                # exposes exception types but for portability we string-match
                # the common patterns.
                msg = str(e).lower()
                retryable = (
                    "rate" in msg
                    or "timeout" in msg
                    or "connection" in msg
                    or "5" in str(getattr(e, "status_code", ""))
                )
                if not retryable or attempt == self.MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt)
        # Unreachable due to raise above, but keeps mypy happy
        raise last_exc  # type: ignore[misc]
