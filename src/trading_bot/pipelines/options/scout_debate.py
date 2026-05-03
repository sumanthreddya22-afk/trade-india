"""Options scout debate (Phase 3) — two-call structure.

Mirrors the crypto scout pattern (``pipelines.crypto.scout_debate``).
Sequential per ADR 0003. One run picks the top-N candidates from
``intel_candidates_options`` and runs them through:

  Call 1 (Sonnet 4.6) — Combined Skeptic + Analyst
    Hank Marquez writes per-underlying skeptic briefs (retail-IV-pump
    detector). Sofia Stevens reads Hank's text in the same context
    window and writes per-underlying analyst briefs (sell-side strategist).
    Single LLM call returns both as ``{"skeptic_briefs": ..., "analyst_briefs": ...}``.

  Call 2 (Opus 4.7) — Judge
    Marcus Whitfield reads both reviewers verbatim and produces the
    audit-of-record verdict per underlying (elevate / dismiss).

Verdict application:
  elevate → ``intel_candidates_options.scout_verdict = 'elevate'``
            (boost applied at score-read time, not stored on the row).
  dismiss → ``scout_dismissed_until = now + dismiss_ttl_hours``. Pool
            readers filter rows where this is in the future.

Failure mode: any exception in either LLM call → SkipVerdict. The
candidate stays in ``scout_verdict = NULL`` state and is re-considered
on the next scout tick. Bot does NOT crash on rate-limit; deterministic-
gates fallback path applies.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.personas import (
    scout_analyst as analyst_persona_mod,
    scout_judge as judge_persona_mod,
    scout_skeptic as skeptic_persona_mod,
)
from trading_bot.pipelines.options.state_db import (
    IntelCandidateOptions,
    ScoutDebateRunOptions,
)
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
class OptionsScoutVerdict:
    underlying: str
    verdict: str            # elevate | dismiss | skipped
    confidence: str
    reason: str
    skeptic_text: str = ""
    analyst_text: str = ""


@dataclass
class OptionsScoutRunResult:
    debated: int
    elevated: int
    dismissed: int
    skipped: int
    verdicts: List[OptionsScoutVerdict] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Reading candidates from the pool
# ---------------------------------------------------------------------------


def select_candidates(
    engine: Any,
    *,
    threshold: float,
    batch_limit: int,
    now: Optional[dt.datetime] = None,
) -> List[IntelCandidateOptions]:
    """Top-N un-debated (or re-debatable) options candidates above ``threshold``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(IntelCandidateOptions)
            .filter(IntelCandidateOptions.score >= threshold)
            .filter(
                (IntelCandidateOptions.scout_dismissed_until.is_(None))
                | (IntelCandidateOptions.scout_dismissed_until <= now)
            )
            .order_by(IntelCandidateOptions.score.desc())
            .limit(batch_limit)
            .all()
        )
        for r in rows:
            session.expunge(r)
    return rows


# ---------------------------------------------------------------------------
# Brief composition
# ---------------------------------------------------------------------------


def _candidates_block(candidates: Sequence[IntelCandidateOptions]) -> str:
    """Render candidates into a compact human-readable block for prompts."""
    lines: List[str] = []
    for c in candidates:
        try:
            sources = json.loads(c.sources_json or "{}")
        except json.JSONDecodeError:
            sources = {}
        sources_str = ", ".join(f"{k}:{v}" for k, v in sorted(sources.items()))
        sentiment = (
            f"{c.sentiment_avg:+.2f}" if c.sentiment_avg is not None else "n/a"
        )
        iv_rank = f"{c.iv_rank:.0f}" if c.iv_rank is not None else "n/a"
        earnings_flag = (
            f"earnings_in_{c.days_to_earnings}d"
            if c.earnings_in_dte_window and c.days_to_earnings is not None
            else "no_earnings_in_window"
        )
        skew = f"{c.cboe_skew:.2f}" if c.cboe_skew is not None else "n/a"
        lines.append(
            f"  - {c.underlying} | score={c.score:.2f} | iv_rank={iv_rank} | "
            f"earnings={earnings_flag} | skew={skew} | "
            f"sources={sources_str} | sentiment_avg={sentiment}\n"
            f"      top: {c.top_reason or '(no headline)'}"
        )
    return "\n".join(lines) if lines else "  (no candidates qualify)"


# ---------------------------------------------------------------------------
# JSON schemas
# ---------------------------------------------------------------------------


_REVIEWER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "skeptic_briefs": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "analyst_briefs": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["skeptic_briefs", "analyst_briefs"],
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
                    "verdict":    {"type": "string", "enum": ["elevate", "dismiss"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason":     {"type": "string"},
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
    candidates: Sequence[IntelCandidateOptions],
    lessons_block: str,
) -> Dict[str, Dict[str, str]]:
    """One Sonnet call producing both skeptic and analyst briefs together."""
    skeptic = parse(skeptic_persona_mod.PERSONA)
    analyst = parse(analyst_persona_mod.PERSONA)

    candidates_block = _candidates_block(candidates)
    skeptic_prompt = render_prompt(
        skeptic,
        candidates_block=candidates_block,
        lessons_block=lessons_block,
    )
    analyst_prompt = render_prompt(
        analyst,
        candidates_block=candidates_block,
        lessons_block=lessons_block,
        skeptic_block="(see skeptic_briefs you produce in this same call)",
    )

    combined_system = (
        "You are running a two-persona options scout debate.\n\n"
        "STEP 1 — Act as Hank Marquez:\n"
        f"{skeptic_prompt}\n\n"
        "STEP 2 — Then, in the SAME response, act as Sofia Stevens reading "
        "Hank's briefs verbatim from above:\n"
        f"{analyst_prompt}\n\n"
        "Return STRICT JSON with two top-level keys:\n"
        '  "skeptic_briefs": {underlying: brief_text}\n'
        '  "analyst_briefs": {underlying: brief_text}\n'
        "Do not return any text outside the JSON object."
    )

    response = transport.complete_structured(
        system=combined_system,
        messages=[{"role": "user", "content": "Produce the two-persona briefs now."}],
        json_schema=_REVIEWER_JSON_SCHEMA,
    )
    payload = _parse_json_payload(response)
    return {
        "skeptic_briefs": payload.get("skeptic_briefs") or {},
        "analyst_briefs": payload.get("analyst_briefs") or {},
    }


def _run_judge_call(
    transport: Any,
    *,
    candidates: Sequence[IntelCandidateOptions],
    skeptic_briefs: Dict[str, str],
    analyst_briefs: Dict[str, str],
    lessons_block: str,
) -> List[Dict[str, str]]:
    """One Opus call producing the verdict-of-record per underlying."""
    judge = parse(judge_persona_mod.PERSONA)
    candidates_block = _candidates_block(candidates)

    def _briefs_to_block(briefs: Dict[str, str]) -> str:
        if not briefs:
            return "  (no briefs produced)"
        return "\n".join(f"  [{sym}] {text}" for sym, text in sorted(briefs.items()))

    judge_system = render_prompt(
        judge,
        skeptic_block=_briefs_to_block(skeptic_briefs),
        analyst_block=_briefs_to_block(analyst_briefs),
        candidates_block=candidates_block,
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
            f"options scout reviewer/judge returned non-JSON: {text[:200]}"
        ) from e


# ---------------------------------------------------------------------------
# Verdict application
# ---------------------------------------------------------------------------


def _apply_verdicts_and_audit(
    engine: Any,
    *,
    verdicts: List[OptionsScoutVerdict],
    candidates_by_symbol: Dict[str, IntelCandidateOptions],
    dismiss_ttl_hours: int,
    prompt_version: str,
    now: dt.datetime,
) -> tuple[int, int, int]:
    elevated = 0
    dismissed = 0
    audit = 0
    dismiss_until = now + dt.timedelta(hours=dismiss_ttl_hours)

    with Session(engine) as session:
        for v in verdicts:
            if v.verdict not in ("elevate", "dismiss"):
                continue
            cand = candidates_by_symbol.get(v.underlying)
            if cand is None:
                logger.warning(
                    "options scout judge produced verdict for unknown underlying %s",
                    v.underlying,
                )
                continue

            stmt = (
                sa_update(IntelCandidateOptions)
                .where(IntelCandidateOptions.underlying == v.underlying)
                .values(
                    scout_verdict=v.verdict,
                    scout_dismissed_until=(
                        dismiss_until if v.verdict == "dismiss" else None
                    ),
                )
            )
            session.execute(stmt)
            if v.verdict == "elevate":
                elevated += 1
            else:
                dismissed += 1

            session.add(ScoutDebateRunOptions(
                run_at=now,
                underlying=v.underlying,
                candidate_score=float(cand.score) if cand.score is not None else None,
                iv_rank=cand.iv_rank,
                earnings_in_dte_window=bool(cand.earnings_in_dte_window),
                top_reason=cand.top_reason or "",
                verdict=v.verdict,
                confidence=v.confidence,
                judge_reason=v.reason,
                skeptic_text=v.skeptic_text,
                analyst_text=v.analyst_text,
                prompt_version=prompt_version,
                synthetic=False,
            ))
            audit += 1
        session.commit()

    return elevated, dismissed, audit


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_scout_debate(
    engine: Any,
    *,
    threshold: float = 3.0,
    batch_limit: int = 25,
    dismiss_ttl_hours: int = 24,
    transport: Any = None,
    now: Optional[dt.datetime] = None,
    lessons_block: Optional[str] = None,
) -> OptionsScoutRunResult:
    """Run one options scout-debate tick. Sequential per ADR 0003.

    ``transport`` is injected for tests; production wires through the
    ``shared.llm_transport`` Claude-CLI subprocess transport.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    candidates = select_candidates(
        engine, threshold=threshold, batch_limit=batch_limit, now=now,
    )
    if not candidates:
        return OptionsScoutRunResult(debated=0, elevated=0, dismissed=0, skipped=0)

    if lessons_block is None:
        try:
            from trading_bot.pipelines.options.lesson_loop import latest_lesson_block
            lessons_block = latest_lesson_block(engine, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning("options scout_debate lesson injection failed: %s", e)
            lessons_block = (
                "(lesson injection failed; debate proceeding without lessons)"
            )

    candidates_by_symbol = {c.underlying: c for c in candidates}

    transport_skeptic = transport or get_transport(
        role_name="options_scout_skeptic", engine=engine,
    )
    transport_judge = transport or get_transport(
        role_name="options_scout_judge", engine=engine,
    )

    # ---- Call 1: combined skeptic + analyst (Sonnet) -----------------
    try:
        briefs = _run_reviewer_call(
            transport_skeptic,
            candidates=candidates,
            lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("options scout_debate skipped: rate-limited (%s)", e)
        return OptionsScoutRunResult(
            debated=len(candidates), elevated=0, dismissed=0,
            skipped=len(candidates), error="rate_limited",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("options scout_debate reviewer call failed: %s", e)
        return OptionsScoutRunResult(
            debated=len(candidates), elevated=0, dismissed=0,
            skipped=len(candidates), error=f"reviewer_error:{e}",
        )

    skeptic_briefs = briefs["skeptic_briefs"]
    analyst_briefs = briefs["analyst_briefs"]

    # ---- Call 2: judge (Opus) ----------------------------------------
    try:
        verdicts_raw = _run_judge_call(
            transport_judge,
            candidates=candidates,
            skeptic_briefs=skeptic_briefs,
            analyst_briefs=analyst_briefs,
            lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("options scout_debate judge skipped: rate-limited (%s)", e)
        return OptionsScoutRunResult(
            debated=len(candidates), elevated=0, dismissed=0,
            skipped=len(candidates), error="rate_limited_judge",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("options scout_debate judge call failed: %s", e)
        return OptionsScoutRunResult(
            debated=len(candidates), elevated=0, dismissed=0,
            skipped=len(candidates), error=f"judge_error:{e}",
        )

    verdicts = [
        OptionsScoutVerdict(
            underlying=v.get("underlying", ""),
            verdict=v.get("verdict", ""),
            confidence=v.get("confidence", "low"),
            reason=v.get("reason", ""),
            skeptic_text=skeptic_briefs.get(v.get("underlying", ""), ""),
            analyst_text=analyst_briefs.get(v.get("underlying", ""), ""),
        )
        for v in verdicts_raw
    ]

    skeptic_obj = parse(skeptic_persona_mod.PERSONA)
    analyst_obj = parse(analyst_persona_mod.PERSONA)
    judge_obj = parse(judge_persona_mod.PERSONA)
    prompt_version = (
        f"options_scout/skeptic={skeptic_obj.prompt_version}"
        f",analyst={analyst_obj.prompt_version}"
        f",judge={judge_obj.prompt_version}"
    )

    elevated, dismissed, _ = _apply_verdicts_and_audit(
        engine,
        verdicts=verdicts,
        candidates_by_symbol=candidates_by_symbol,
        dismiss_ttl_hours=dismiss_ttl_hours,
        prompt_version=prompt_version,
        now=now,
    )

    return OptionsScoutRunResult(
        debated=len(candidates),
        elevated=elevated,
        dismissed=dismissed,
        skipped=len(candidates) - elevated - dismissed,
        verdicts=verdicts,
    )
