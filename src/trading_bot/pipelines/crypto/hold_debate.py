"""Crypto hold debate (Phase 1C) — two-call structure.

Sequential per ADR 0003. One run takes a list of TriggerContexts (built
by ``position_monitor.classify_triggers``) and runs them through:

  Call 1 (Sonnet 4.6) — Combined Aggressive + Conservative + Neutral
    Marcus / James / Priya each produce per-position briefs.
    The Conservative reads Aggressive's briefs verbatim from the same
    context window; the Neutral reads both prior reviewers'. Single
    structured-JSON return.

  Call 2 (Opus 4.7) — Judge
    Diane Pereira reads all three reviewer outputs verbatim and
    produces the audit-of-record verdict: hold | tighten_stop | exit_now.

Verdict application is callback-based — this module computes the
verdict and persists the audit row, but the caller (position_monitor)
plugs in the broker action (``replace_stop`` / ``flatten_position``)
because broker access is shared/alpaca_client and not part of this
module's contract.

Failure mode: any exception in either LLM call → SkipVerdict
('rate_limited' or generic error). Position is left untouched —
brackets stay live, hold debate retries on next monitor tick.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.personas import (
    hold_aggressive as aggressive_persona_mod,
    hold_conservative as conservative_persona_mod,
    hold_judge as judge_persona_mod,
    hold_neutral as neutral_persona_mod,
)
from trading_bot.pipelines.crypto.state_db import HoldDebateRunCrypto
from trading_bot.shared.llm_transport import (
    LlmResponse,
    LlmTransportError,
    SubscriptionRateLimited,
    get_transport,
)
from trading_bot.shared.personas._base import parse, render_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data carried into the debate (built by position_monitor.classify_triggers)
# ---------------------------------------------------------------------------


@dataclass
class TriggerContext:
    """Everything the personas need to reason about ONE held position."""
    symbol: str
    entry_order_id: Optional[str]
    trigger_reason: str        # one of position_monitor's trigger names
    trigger_evidence: str      # human-readable evidence detail
    # Position state
    side: str = "long"
    qty: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    pnl_pct: float = 0.0
    days_held: float = 0.0
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    # Intel snapshot (entry baseline vs now)
    entry_score: Optional[float] = None
    current_score: Optional[float] = None
    entry_sentiment: Optional[float] = None
    current_sentiment: Optional[float] = None
    # Free-form chain context
    chain: Optional[str] = None


@dataclass
class HoldVerdict:
    symbol: str
    verdict: str            # hold | tighten_stop | exit_now | skipped
    confidence: str
    reason: str
    aggressive_text: str = ""
    conservative_text: str = ""
    neutral_text: str = ""
    new_stop_price: Optional[float] = None  # only meaningful for tighten_stop


@dataclass
class HoldRunResult:
    debated: int
    held: int
    tightened: int
    exited: int
    skipped: int
    verdicts: List[HoldVerdict] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _position_block(ctx: TriggerContext) -> str:
    return (
        f"  - {ctx.symbol} | side={ctx.side} qty={ctx.qty} "
        f"entry={ctx.entry_price:.4f} current={ctx.current_price:.4f} "
        f"pnl_pct={ctx.pnl_pct:+.2f}% days_held={ctx.days_held:.1f}\n"
        f"      stop={ctx.stop_price} tp={ctx.take_profit_price} chain={ctx.chain or 'n/a'}"
    )


def _trigger_block(ctx: TriggerContext) -> str:
    snapshot = ""
    if ctx.entry_score is not None and ctx.current_score is not None:
        snapshot += (
            f"\n      intel score: {ctx.entry_score:.2f} (entry) → "
            f"{ctx.current_score:.2f} (now)"
        )
    if ctx.entry_sentiment is not None and ctx.current_sentiment is not None:
        snapshot += (
            f"\n      sentiment_avg: {ctx.entry_sentiment:+.2f} (entry) → "
            f"{ctx.current_sentiment:+.2f} (now)"
        )
    return (
        f"  - {ctx.symbol} trigger={ctx.trigger_reason}\n"
        f"      evidence: {ctx.trigger_evidence}{snapshot}"
    )


def _render_blocks(triggers: Sequence[TriggerContext]) -> tuple[str, str]:
    if not triggers:
        return "  (no positions under review)", "  (no triggers)"
    return (
        "\n".join(_position_block(c) for c in triggers),
        "\n".join(_trigger_block(c) for c in triggers),
    )


# ---------------------------------------------------------------------------
# JSON schemas — drive structured output from both LLM calls
# ---------------------------------------------------------------------------


_REVIEWER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "aggressive_briefs":   {"type": "object", "additionalProperties": {"type": "string"}},
        "conservative_briefs": {"type": "object", "additionalProperties": {"type": "string"}},
        "neutral_briefs":      {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["aggressive_briefs", "conservative_briefs", "neutral_briefs"],
}


_JUDGE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol":     {"type": "string"},
                    "verdict":    {"type": "string", "enum": ["hold", "tighten_stop", "exit_now"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason":     {"type": "string"},
                    "new_stop_price": {"type": ["number", "null"]},
                },
                "required": ["symbol", "verdict", "confidence", "reason"],
            },
        },
    },
    "required": ["verdicts"],
}


# ---------------------------------------------------------------------------
# LLM call orchestration
# ---------------------------------------------------------------------------


def _run_reviewer_call(
    transport: Any,
    *,
    triggers: Sequence[TriggerContext],
    lessons_block: str,
) -> Dict[str, Dict[str, str]]:
    aggressive = parse(aggressive_persona_mod.PERSONA)
    conservative = parse(conservative_persona_mod.PERSONA)
    neutral = parse(neutral_persona_mod.PERSONA)
    position_block, trigger_block = _render_blocks(triggers)

    aggressive_prompt = render_prompt(
        aggressive,
        position_block=position_block,
        trigger_block=trigger_block,
        lessons_block=lessons_block,
    )
    conservative_prompt = render_prompt(
        conservative,
        aggressive_block="(see aggressive_briefs you produce in this same call)",
        position_block=position_block,
        trigger_block=trigger_block,
        lessons_block=lessons_block,
    )
    neutral_prompt = render_prompt(
        neutral,
        aggressive_block="(see aggressive_briefs you produce above)",
        conservative_block="(see conservative_briefs you produce above)",
        position_block=position_block,
        trigger_block=trigger_block,
        lessons_block=lessons_block,
    )

    combined_system = (
        "You are running a three-persona crypto hold debate.\n\n"
        "STEP 1 — Act as Marcus Reid:\n"
        f"{aggressive_prompt}\n\n"
        "STEP 2 — Then in the SAME response, act as James Chen reading "
        "Marcus's briefs verbatim from above:\n"
        f"{conservative_prompt}\n\n"
        "STEP 3 — Then in the SAME response, act as Priya Anand reading "
        "BOTH Marcus's and James's briefs verbatim from above:\n"
        f"{neutral_prompt}\n\n"
        "Return STRICT JSON with three top-level keys:\n"
        '  "aggressive_briefs":   {symbol: brief_text}\n'
        '  "conservative_briefs": {symbol: brief_text}\n'
        '  "neutral_briefs":      {symbol: brief_text}\n'
        "Do not return any text outside the JSON object."
    )

    response = transport.complete_structured(
        system=combined_system,
        messages=[{"role": "user", "content": "Produce the three-persona briefs now."}],
        json_schema=_REVIEWER_JSON_SCHEMA,
    )
    payload = _parse_json_payload(response)
    return {
        "aggressive_briefs":   payload.get("aggressive_briefs") or {},
        "conservative_briefs": payload.get("conservative_briefs") or {},
        "neutral_briefs":      payload.get("neutral_briefs") or {},
    }


def _run_judge_call(
    transport: Any,
    *,
    triggers: Sequence[TriggerContext],
    aggressive_briefs: Dict[str, str],
    conservative_briefs: Dict[str, str],
    neutral_briefs: Dict[str, str],
    lessons_block: str,
) -> List[Dict[str, Any]]:
    judge = parse(judge_persona_mod.PERSONA)
    position_block, trigger_block = _render_blocks(triggers)

    def _briefs_to_block(briefs: Dict[str, str]) -> str:
        if not briefs:
            return "  (no briefs produced)"
        return "\n".join(f"  [{sym}] {text}" for sym, text in sorted(briefs.items()))

    judge_system = render_prompt(
        judge,
        aggressive_block=_briefs_to_block(aggressive_briefs),
        conservative_block=_briefs_to_block(conservative_briefs),
        neutral_block=_briefs_to_block(neutral_briefs),
        position_block=position_block,
        trigger_block=trigger_block,
        lessons_block=lessons_block,
    )

    response = transport.complete_structured(
        system=judge_system,
        messages=[{"role": "user", "content": "Produce verdicts as strict JSON now."}],
        json_schema=_JUDGE_JSON_SCHEMA,
    )
    payload = _parse_json_payload(response)
    return payload.get("verdicts") or []


def _parse_json_payload(response: LlmResponse) -> Dict[str, Any]:
    raw_result = response.raw.get("result") if isinstance(response.raw, dict) else None
    if isinstance(raw_result, dict):
        return raw_result
    text = (response.text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LlmTransportError(f"hold reviewer/judge returned non-JSON: {text[:200]}") from e


# ---------------------------------------------------------------------------
# Audit + action plumbing
# ---------------------------------------------------------------------------


def _persist_audit(
    engine: Any,
    *,
    verdict: HoldVerdict,
    ctx: TriggerContext,
    action_taken: str,
    prompt_version: str,
    now: dt.datetime,
) -> None:
    with Session(engine) as session:
        session.add(HoldDebateRunCrypto(
            run_at=now,
            symbol=verdict.symbol,
            entry_order_id=ctx.entry_order_id,
            trigger_reason=ctx.trigger_reason,
            current_score=ctx.current_score,
            current_sentiment=ctx.current_sentiment,
            entry_score=ctx.entry_score,
            entry_sentiment=ctx.entry_sentiment,
            verdict=verdict.verdict,
            confidence=verdict.confidence,
            judge_reason=verdict.reason,
            aggressive_text=verdict.aggressive_text,
            conservative_text=verdict.conservative_text,
            neutral_text=verdict.neutral_text,
            action_taken=action_taken,
            resulting_pnl_pct=None,  # backfilled when position closes
            prompt_version=prompt_version,
            synthetic=False,
        ))
        session.commit()


# ---------------------------------------------------------------------------
# Action executor signature — caller plugs in alpaca_client wrappers
# ---------------------------------------------------------------------------


class HoldActionExecutor:
    """Pluggable action executor — plug in real alpaca_client wrappers in
    production; pass a fake one in tests so we don't hit the broker.
    """
    def replace_stop(self, *, symbol: str, new_stop_price: float) -> None: ...
    def flatten_position(self, *, symbol: str) -> None: ...


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_hold_debate(
    engine: Any,
    *,
    triggers: Sequence[TriggerContext],
    executor: Optional[HoldActionExecutor] = None,
    transport: Any = None,
    now: Optional[dt.datetime] = None,
    lessons_block: Optional[str] = None,
) -> HoldRunResult:
    """Run one crypto hold-debate tick over the supplied trigger contexts.

    ``executor`` is None-safe: when omitted, verdicts are computed +
    audited but no broker action is taken (useful for dry-run or
    unit tests). Production callers (position_monitor) pass a wrapper
    that delegates to ``shared.alpaca_client.replace_stop`` /
    ``flatten_position`` via the optimistic-concurrency submit_txn.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if not triggers:
        return HoldRunResult(debated=0, held=0, tightened=0, exited=0, skipped=0)

    triggers = list(triggers)

    # Phase 1D — pull the freshest lesson block when the caller didn't
    # override. Fail-soft: if the lesson loop blew up, fall back to the
    # placeholder rather than refuse to debate.
    if lessons_block is None:
        try:
            from trading_bot.pipelines.crypto.lesson_loop import latest_lesson_block
            lessons_block = latest_lesson_block(engine, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning("hold_debate lesson injection failed: %s", e)
            lessons_block = "(lesson injection failed; debate proceeding without lessons)"
    triggers_by_symbol = {t.symbol: t for t in triggers}

    transport_reviewer = transport or get_transport(role_name="hold_aggressive", engine=engine)
    transport_judge = transport or get_transport(role_name="hold_judge", engine=engine)

    # ---- Call 1: combined three reviewers (Sonnet) -------------------
    try:
        briefs = _run_reviewer_call(
            transport_reviewer, triggers=triggers, lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("hold_debate skipped: rate-limited (%s)", e)
        return HoldRunResult(
            debated=len(triggers), held=0, tightened=0, exited=0,
            skipped=len(triggers), error="rate_limited",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("hold_debate reviewer call failed")
        return HoldRunResult(
            debated=len(triggers), held=0, tightened=0, exited=0,
            skipped=len(triggers), error=f"reviewer_error:{e}",
        )

    aggressive_briefs = briefs["aggressive_briefs"]
    conservative_briefs = briefs["conservative_briefs"]
    neutral_briefs = briefs["neutral_briefs"]

    # ---- Call 2: judge (Opus) ----------------------------------------
    try:
        verdicts_raw = _run_judge_call(
            transport_judge,
            triggers=triggers,
            aggressive_briefs=aggressive_briefs,
            conservative_briefs=conservative_briefs,
            neutral_briefs=neutral_briefs,
            lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("hold_debate judge skipped: rate-limited (%s)", e)
        return HoldRunResult(
            debated=len(triggers), held=0, tightened=0, exited=0,
            skipped=len(triggers), error="rate_limited_judge",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("hold_debate judge call failed")
        return HoldRunResult(
            debated=len(triggers), held=0, tightened=0, exited=0,
            skipped=len(triggers), error=f"judge_error:{e}",
        )

    # ---- Build verdicts + apply ---------------------------------------
    aggressive_obj = parse(aggressive_persona_mod.PERSONA)
    conservative_obj = parse(conservative_persona_mod.PERSONA)
    neutral_obj = parse(neutral_persona_mod.PERSONA)
    judge_obj = parse(judge_persona_mod.PERSONA)
    prompt_version = (
        f"crypto_hold/aggressive={aggressive_obj.prompt_version}"
        f",conservative={conservative_obj.prompt_version}"
        f",neutral={neutral_obj.prompt_version}"
        f",judge={judge_obj.prompt_version}"
    )

    held = tightened = exited = 0
    out_verdicts: List[HoldVerdict] = []

    for v in verdicts_raw:
        symbol = v.get("symbol", "")
        ctx = triggers_by_symbol.get(symbol)
        if ctx is None:
            logger.warning("hold judge produced verdict for unknown symbol %s", symbol)
            continue

        verdict_str = v.get("verdict", "hold")
        verdict = HoldVerdict(
            symbol=symbol,
            verdict=verdict_str,
            confidence=v.get("confidence", "low"),
            reason=v.get("reason", ""),
            aggressive_text=aggressive_briefs.get(symbol, ""),
            conservative_text=conservative_briefs.get(symbol, ""),
            neutral_text=neutral_briefs.get(symbol, ""),
            new_stop_price=v.get("new_stop_price"),
        )
        out_verdicts.append(verdict)

        action_taken = "none"
        if executor is not None:
            try:
                if verdict_str == "tighten_stop" and verdict.new_stop_price is not None:
                    executor.replace_stop(symbol=symbol, new_stop_price=verdict.new_stop_price)
                    action_taken = "stop_replaced"
                elif verdict_str == "exit_now":
                    executor.flatten_position(symbol=symbol)
                    action_taken = "flattened"
            except Exception as e:  # noqa: BLE001 — broker error never crashes audit
                logger.exception("hold action failed for %s: %s", symbol, e)
                action_taken = f"action_error:{type(e).__name__}"

        _persist_audit(
            engine, verdict=verdict, ctx=ctx, action_taken=action_taken,
            prompt_version=prompt_version, now=now,
        )

        if verdict_str == "hold":
            held += 1
        elif verdict_str == "tighten_stop":
            tightened += 1
        elif verdict_str == "exit_now":
            exited += 1

    return HoldRunResult(
        debated=len(triggers),
        held=held,
        tightened=tightened,
        exited=exited,
        skipped=len(triggers) - held - tightened - exited,
        verdicts=out_verdicts,
    )
