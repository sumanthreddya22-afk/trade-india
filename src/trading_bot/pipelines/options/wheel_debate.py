"""Options wheel-entry debate (Phase 3) — three-reviewer + judge.

Mirrors the crypto entry debate (``pipelines.crypto.entry_debate``) but
specialised for the wheel structure:

  Call 1 (Sonnet 4.6) — Combined Aggressive + Conservative + Neutral
    Aurelio Ortiz writes per-candidate aggressive briefs (high-delta CSP
    bias, tolerates assignment).
    Beatrice Wagner reads Aurelio's text in the same context window and
    writes per-candidate conservative briefs (low-delta, income-focused).
    Yusuf Hassan reads both prior reviewers and writes the macro-overlay
    neutral briefs.
    Single LLM call returns all three sets of briefs.

  Call 2 (Opus 4.7) — Judge
    Catherine Lloyd reads all three reviewers verbatim and produces:
      verdict (place / skip / defer_restale)
      chosen_delta + chosen_dte_days + chosen_structure
      audit-ready ``judge_reason``

Verdict application is callback-based — this module computes the verdict
and persists the audit row, but the broker action is injected via
``WheelOrderExecutor.submit_wheel_entry``. Production wires through the
existing ``options.alpaca_options.OptionAlpacaClient.submit_short_put``
(or short_call) via shared/submit_txn.

Failure mode: any LLM exception → SkipVerdict. Caller must NOT submit
on a non-place verdict.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from trading_bot.pipelines.options.personas import (
    wheel_aggressive as aggressive_persona_mod,
    wheel_conservative as conservative_persona_mod,
    wheel_judge as judge_persona_mod,
    wheel_neutral as neutral_persona_mod,
)
from trading_bot.pipelines.options.state_db import WheelDebateRunOptions
from trading_bot.shared.llm_transport import (
    LlmResponse,
    LlmTransportError,
    SubscriptionRateLimited,
    get_transport,
)
from trading_bot.shared.personas._base import parse, render_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class WheelCandidate:
    """One wheel-entry candidate with proposed structure parameters."""
    underlying: str
    candidate_score: Optional[float] = None
    iv_rank: Optional[float] = None
    intel_top_reason: str = ""
    sentiment_avg: Optional[float] = None
    proposed_strike: float = 0.0
    proposed_delta: float = 0.0
    proposed_dte_days: int = 0
    proposed_structure: str = "csp"  # csp | cc | vertical | cash
    earnings_in_dte_window: bool = False
    days_to_earnings: Optional[int] = None


@dataclass
class WheelVerdict:
    underlying: str
    verdict: str            # place | skip | defer_restale
    confidence: str
    reason: str
    chosen_delta: Optional[float] = None
    chosen_dte_days: Optional[int] = None
    chosen_structure: Optional[str] = None
    aggressive_text: str = ""
    conservative_text: str = ""
    neutral_text: str = ""


@dataclass
class WheelRunResult:
    debated: int
    placed: int
    skipped: int
    deferred: int
    verdicts: List[WheelVerdict] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Brief composition
# ---------------------------------------------------------------------------


def _order_block(candidates: Sequence[WheelCandidate]) -> str:
    if not candidates:
        return "  (no candidates)"
    return "\n".join(
        f"  - {c.underlying} | structure={c.proposed_structure} "
        f"strike={c.proposed_strike:.2f} delta={c.proposed_delta:.2f} "
        f"dte={c.proposed_dte_days}d"
        for c in candidates
    )


def _intel_block(candidates: Sequence[WheelCandidate]) -> str:
    if not candidates:
        return "  (no candidates)"
    out: List[str] = []
    for c in candidates:
        sentiment = (
            f"{c.sentiment_avg:+.2f}" if c.sentiment_avg is not None else "n/a"
        )
        iv_rank = f"{c.iv_rank:.0f}" if c.iv_rank is not None else "n/a"
        score = f"{c.candidate_score:.2f}" if c.candidate_score is not None else "n/a"
        earnings = (
            f"earnings_in_{c.days_to_earnings}d"
            if c.earnings_in_dte_window and c.days_to_earnings is not None
            else "no_earnings_in_dte"
        )
        out.append(
            f"  - {c.underlying} | score={score} | iv_rank={iv_rank} | "
            f"{earnings} | sentiment_avg={sentiment}\n"
            f"      top: {c.intel_top_reason or '(no headline)'}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# JSON schemas
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
                    "underlying": {"type": "string"},
                    "verdict":    {"type": "string", "enum": ["place", "skip", "defer_restale"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason":     {"type": "string"},
                    "chosen_delta":     {"type": ["number", "null"]},
                    "chosen_dte_days":  {"type": ["integer", "null"]},
                    "chosen_structure": {"type": ["string", "null"]},
                },
                "required": ["underlying", "verdict", "confidence", "reason"],
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
    candidates: Sequence[WheelCandidate],
    regime: str,
    lessons_block: str,
) -> Dict[str, Dict[str, str]]:
    aggressive = parse(aggressive_persona_mod.PERSONA)
    conservative = parse(conservative_persona_mod.PERSONA)
    neutral = parse(neutral_persona_mod.PERSONA)

    order_block = _order_block(candidates)
    intel_block = _intel_block(candidates)

    aggressive_prompt = render_prompt(
        aggressive,
        order_block=order_block, intel_block=intel_block,
        regime=regime, lessons_block=lessons_block,
    )
    conservative_prompt = render_prompt(
        conservative,
        aggressive_block="(see aggressive_briefs you produce in this same call)",
        order_block=order_block, intel_block=intel_block,
        regime=regime, lessons_block=lessons_block,
    )
    neutral_prompt = render_prompt(
        neutral,
        aggressive_block="(see aggressive_briefs you produce above)",
        conservative_block="(see conservative_briefs you produce above)",
        order_block=order_block, intel_block=intel_block,
        regime=regime, lessons_block=lessons_block,
    )

    combined_system = (
        "You are running a three-persona options wheel-entry debate.\n\n"
        "STEP 1 — Act as Aurelio Ortiz:\n"
        f"{aggressive_prompt}\n\n"
        "STEP 2 — Then in the SAME response, act as Beatrice Wagner reading "
        "Aurelio's briefs verbatim from above:\n"
        f"{conservative_prompt}\n\n"
        "STEP 3 — Then in the SAME response, act as Yusuf Hassan reading "
        "BOTH Aurelio's and Beatrice's briefs verbatim from above:\n"
        f"{neutral_prompt}\n\n"
        "Return STRICT JSON with three top-level keys:\n"
        '  "aggressive_briefs":   {underlying: brief_text}\n'
        '  "conservative_briefs": {underlying: brief_text}\n'
        '  "neutral_briefs":      {underlying: brief_text}\n'
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
    candidates: Sequence[WheelCandidate],
    aggressive_briefs: Dict[str, str],
    conservative_briefs: Dict[str, str],
    neutral_briefs: Dict[str, str],
    regime: str,
    lessons_block: str,
) -> List[Dict[str, Any]]:
    judge = parse(judge_persona_mod.PERSONA)
    order_block = _order_block(candidates)
    intel_block = _intel_block(candidates)

    def _briefs_to_block(briefs: Dict[str, str]) -> str:
        if not briefs:
            return "  (no briefs produced)"
        return "\n".join(f"  [{sym}] {text}" for sym, text in sorted(briefs.items()))

    judge_system = render_prompt(
        judge,
        aggressive_block=_briefs_to_block(aggressive_briefs),
        conservative_block=_briefs_to_block(conservative_briefs),
        neutral_block=_briefs_to_block(neutral_briefs),
        order_block=order_block,
        intel_block=intel_block,
        regime=regime,
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
        raise LlmTransportError(
            f"options wheel reviewer/judge returned non-JSON: {text[:200]}"
        ) from e


# ---------------------------------------------------------------------------
# Audit + execution plumbing
# ---------------------------------------------------------------------------


def _persist_audit(
    engine: Any,
    *,
    verdict: WheelVerdict,
    candidate: WheelCandidate,
    regime: str,
    entry_order_id: Optional[str],
    cycle_id: Optional[int],
    prompt_version: str,
    now: dt.datetime,
) -> None:
    with Session(engine) as session:
        session.add(WheelDebateRunOptions(
            run_at=now,
            underlying=verdict.underlying,
            candidate_score=candidate.candidate_score,
            iv_rank=candidate.iv_rank,
            proposed_delta=candidate.proposed_delta,
            proposed_dte_days=candidate.proposed_dte_days,
            proposed_strike=candidate.proposed_strike,
            regime=regime,
            verdict=verdict.verdict,
            confidence=verdict.confidence,
            chosen_delta=verdict.chosen_delta,
            chosen_dte_days=verdict.chosen_dte_days,
            chosen_structure=verdict.chosen_structure,
            judge_reason=verdict.reason,
            aggressive_text=verdict.aggressive_text,
            conservative_text=verdict.conservative_text,
            neutral_text=verdict.neutral_text,
            entry_order_id=entry_order_id,
            cycle_id=cycle_id,
            prompt_version=prompt_version,
            synthetic=False,
        ))
        session.commit()


class WheelOrderExecutor:
    """Pluggable broker call. Production wires through shared.submit_txn +
    options.alpaca_options.OptionAlpacaClient.submit_short_put / short_call.
    """

    def submit_wheel_entry(
        self,
        *,
        candidate: WheelCandidate,
        chosen_delta: float,
        chosen_dte_days: int,
        chosen_structure: str,
    ) -> tuple[Optional[str], Optional[int]]:
        """Returns (broker_order_id, cycle_id) on success, (None, None) on rejection."""
        ...


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_wheel_debate(
    engine: Any,
    *,
    candidates: Sequence[WheelCandidate],
    regime: str = "neutral_vol",
    executor: Optional[WheelOrderExecutor] = None,
    transport: Any = None,
    now: Optional[dt.datetime] = None,
    lessons_block: Optional[str] = None,
) -> WheelRunResult:
    """Run one options wheel-entry-debate tick over the supplied candidates.

    ``executor`` is None-safe: when omitted, verdicts are computed +
    audited but no orders are submitted (useful for dry-run / unit
    tests). Production callers pass a wrapper that delegates to the
    OptionAlpacaClient via the optimistic-concurrency submit_txn.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if not candidates:
        return WheelRunResult(debated=0, placed=0, skipped=0, deferred=0)

    candidates = list(candidates)
    candidates_by_symbol = {c.underlying: c for c in candidates}

    if lessons_block is None:
        try:
            from trading_bot.pipelines.options.lesson_loop import latest_lesson_block
            lessons_block = latest_lesson_block(engine, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning("options wheel_debate lesson injection failed: %s", e)
            lessons_block = (
                "(lesson injection failed; debate proceeding without lessons)"
            )

    transport_reviewer = transport or get_transport(
        role_name="options_wheel_aggressive", engine=engine,
    )
    transport_judge = transport or get_transport(
        role_name="options_wheel_judge", engine=engine,
    )

    # ---- Call 1: combined three reviewers (Sonnet) -------------------
    try:
        briefs = _run_reviewer_call(
            transport_reviewer, candidates=candidates, regime=regime,
            lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("options wheel_debate skipped: rate-limited (%s)", e)
        return WheelRunResult(
            debated=len(candidates), placed=0, skipped=0,
            deferred=len(candidates), error="rate_limited",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("options wheel_debate reviewer call failed")
        return WheelRunResult(
            debated=len(candidates), placed=0, skipped=0,
            deferred=len(candidates), error=f"reviewer_error:{e}",
        )

    aggressive_briefs = briefs["aggressive_briefs"]
    conservative_briefs = briefs["conservative_briefs"]
    neutral_briefs = briefs["neutral_briefs"]

    # ---- Call 2: judge (Opus) ----------------------------------------
    try:
        verdicts_raw = _run_judge_call(
            transport_judge,
            candidates=candidates,
            aggressive_briefs=aggressive_briefs,
            conservative_briefs=conservative_briefs,
            neutral_briefs=neutral_briefs,
            regime=regime,
            lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("options wheel_debate judge skipped: rate-limited (%s)", e)
        return WheelRunResult(
            debated=len(candidates), placed=0, skipped=0,
            deferred=len(candidates), error="rate_limited_judge",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("options wheel_debate judge call failed")
        return WheelRunResult(
            debated=len(candidates), placed=0, skipped=0,
            deferred=len(candidates), error=f"judge_error:{e}",
        )

    # ---- Build verdicts + apply ---------------------------------------
    aggressive_obj = parse(aggressive_persona_mod.PERSONA)
    conservative_obj = parse(conservative_persona_mod.PERSONA)
    neutral_obj = parse(neutral_persona_mod.PERSONA)
    judge_obj = parse(judge_persona_mod.PERSONA)
    prompt_version = (
        f"options_wheel/aggressive={aggressive_obj.prompt_version}"
        f",conservative={conservative_obj.prompt_version}"
        f",neutral={neutral_obj.prompt_version}"
        f",judge={judge_obj.prompt_version}"
    )

    placed = skipped = deferred = 0
    out_verdicts: List[WheelVerdict] = []

    for v in verdicts_raw:
        underlying = v.get("underlying", "")
        cand = candidates_by_symbol.get(underlying)
        if cand is None:
            logger.warning(
                "options wheel judge produced verdict for unknown underlying %s",
                underlying,
            )
            continue

        verdict_str = v.get("verdict", "skip")
        chosen_delta = v.get("chosen_delta")
        chosen_dte = v.get("chosen_dte_days")
        chosen_struct = v.get("chosen_structure")
        if verdict_str == "place":
            chosen_delta = chosen_delta if chosen_delta is not None else cand.proposed_delta
            chosen_dte = chosen_dte if chosen_dte is not None else cand.proposed_dte_days
            chosen_struct = chosen_struct or cand.proposed_structure

        verdict = WheelVerdict(
            underlying=underlying, verdict=verdict_str,
            confidence=v.get("confidence", "low"),
            reason=v.get("reason", ""),
            chosen_delta=chosen_delta,
            chosen_dte_days=chosen_dte,
            chosen_structure=chosen_struct,
            aggressive_text=aggressive_briefs.get(underlying, ""),
            conservative_text=conservative_briefs.get(underlying, ""),
            neutral_text=neutral_briefs.get(underlying, ""),
        )
        out_verdicts.append(verdict)

        entry_order_id: Optional[str] = None
        cycle_id: Optional[int] = None
        if (
            executor is not None
            and verdict_str == "place"
            and chosen_delta is not None
            and chosen_dte is not None
            and chosen_struct is not None
        ):
            try:
                entry_order_id, cycle_id = executor.submit_wheel_entry(
                    candidate=cand,
                    chosen_delta=float(chosen_delta),
                    chosen_dte_days=int(chosen_dte),
                    chosen_structure=str(chosen_struct),
                )
            except Exception as e:  # noqa: BLE001 — broker error never crashes audit
                logger.exception("options wheel submit failed for %s: %s", underlying, e)
                entry_order_id = None
                cycle_id = None

        _persist_audit(
            engine, verdict=verdict, candidate=cand, regime=regime,
            entry_order_id=entry_order_id, cycle_id=cycle_id,
            prompt_version=prompt_version, now=now,
        )

        if verdict_str == "place":
            placed += 1
        elif verdict_str == "skip":
            skipped += 1
        elif verdict_str == "defer_restale":
            deferred += 1

    return WheelRunResult(
        debated=len(candidates),
        placed=placed, skipped=skipped, deferred=deferred,
        verdicts=out_verdicts,
    )
