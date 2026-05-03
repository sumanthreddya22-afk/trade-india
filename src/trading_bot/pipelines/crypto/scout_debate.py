"""Crypto scout debate (Phase 1B) — two-call structure.

Sequential per ADR 0003. One run picks the top-N candidates from
``intel_candidates_crypto`` and runs them through:

  Call 1 (Sonnet 4.6) — Combined Skeptic + Analyst
    Sasha Volkov writes per-symbol skeptic briefs.
    Lena Park reads Sasha's text in the same context window and writes
    per-symbol analyst briefs that engage Sasha's concerns.
    The single LLM call returns ``{"skeptic_briefs": {...}, "analyst_briefs": {...}}``.

  Call 2 (Opus 4.7) — Judge
    Diane Pereira reads Sasha + Lena's briefs verbatim and produces the
    audit-of-record verdict per symbol (elevate / dismiss).

Verdict application:
  elevate → ``intel_candidates_crypto.scout_verdict = 'elevate'`` (boost
            applied at score-read time, not stored on the row to keep
            audit clean).
  dismiss → ``scout_dismissed_until = now + dismiss_ttl_hours``. Pool
            readers filter rows where this is in the future.

Override path: ``override_dismissals_for_whale_alert`` re-elevates
dismissed symbols when a confirmed whale_alert event lands within the
dismissal window.

Failure mode: any exception in either LLM call → SkipVerdict ('skipped';
no verdict written). The candidate stays in ``scout_verdict = NULL``
state and is re-considered on the next scout tick. Bot does NOT crash
when the LLM transport rate-limits; deterministic-gates fallback path
applies.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.personas import (
    scout_analyst as analyst_persona_mod,
    scout_judge as judge_persona_mod,
    scout_skeptic as skeptic_persona_mod,
)
from trading_bot.pipelines.crypto.state_db import (
    IntelCandidateCrypto,
    ScoutDebateRunCrypto,
)
from trading_bot.shared.llm_transport import (
    LlmResponse,
    SubscriptionRateLimited,
    LlmTransportError,
    get_transport,
)
from trading_bot.shared.personas._base import parse, render_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ScoutVerdict:
    symbol: str
    verdict: str            # elevate | dismiss | skipped
    confidence: str         # high | medium | low
    reason: str
    skeptic_text: str = ""
    analyst_text: str = ""


@dataclass
class ScoutRunResult:
    debated: int
    elevated: int
    dismissed: int
    skipped: int
    verdicts: List[ScoutVerdict] = field(default_factory=list)
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
) -> List[IntelCandidateCrypto]:
    """Pick the top-N un-debated (or re-debatable) candidates above ``threshold``.

    Filter: ``score >= threshold`` AND (``scout_verdict IS NULL`` OR
    ``scout_dismissed_until IS NULL`` OR ``scout_dismissed_until <= now``).
    Order: highest score first.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(IntelCandidateCrypto)
            .filter(IntelCandidateCrypto.score >= threshold)
            .filter(
                (IntelCandidateCrypto.scout_dismissed_until.is_(None))
                | (IntelCandidateCrypto.scout_dismissed_until <= now)
            )
            .order_by(IntelCandidateCrypto.score.desc())
            .limit(batch_limit)
            .all()
        )
        # Detach so the caller can read fields after the session closes.
        for r in rows:
            session.expunge(r)
    return rows


# ---------------------------------------------------------------------------
# Brief composition
# ---------------------------------------------------------------------------


def _candidates_block(candidates: Sequence[IntelCandidateCrypto]) -> str:
    """Render candidates into a compact human-readable block for the prompts."""
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
        flags: List[str] = []
        for f in ("suspicious_spike", "coordinated", "pump_signature",
                  "cold_start_token", "whale_concentration",
                  "honeypot_detected", "sybil_coordinated"):
            if getattr(c, f, False):
                flags.append(f)
        flag_str = ", ".join(flags) if flags else "none"
        lines.append(
            f"  - {c.symbol} | score={c.score:.2f} | "
            f"sources={sources_str} | sentiment_avg={sentiment} | "
            f"adversarial_flags={flag_str}\n"
            f"      top: {c.top_reason or '(no headline)'}"
        )
    return "\n".join(lines) if lines else "  (no candidates qualify)"


# ---------------------------------------------------------------------------
# LLM call orchestration
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
                    "symbol":     {"type": "string"},
                    "verdict":    {"type": "string", "enum": ["elevate", "dismiss"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason":     {"type": "string"},
                },
                "required": ["symbol", "verdict", "confidence", "reason"],
            },
        },
    },
    "required": ["verdicts"],
}


def _run_reviewer_call(
    transport: Any,
    *,
    candidates: Sequence[IntelCandidateCrypto],
    lessons_block: str,
) -> Dict[str, Dict[str, str]]:
    """One Sonnet call producing both skeptic and analyst briefs together.

    Returns ``{"skeptic_briefs": {sym: text}, "analyst_briefs": {sym: text}}``.
    """
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

    # Compose both personas into one combined system prompt so a single
    # Sonnet call produces both outputs (Phase 1A.11 batching strategy).
    combined_system = (
        "You are running a two-persona crypto scout debate.\n\n"
        "STEP 1 — Act as Sasha Volkov:\n"
        f"{skeptic_prompt}\n\n"
        "STEP 2 — Then, in the SAME response, act as Lena Park reading "
        "Sasha's briefs verbatim from above:\n"
        f"{analyst_prompt}\n\n"
        "Return STRICT JSON with two top-level keys:\n"
        '  "skeptic_briefs": {symbol: brief_text}\n'
        '  "analyst_briefs": {symbol: brief_text}\n'
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
    candidates: Sequence[IntelCandidateCrypto],
    skeptic_briefs: Dict[str, str],
    analyst_briefs: Dict[str, str],
    lessons_block: str,
) -> List[Dict[str, str]]:
    """One Opus call producing the verdict-of-record per symbol."""
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
    """Extract dict from either ``response.text`` or ``response.raw['result']``.

    With ``--json-schema`` the CLI returns the validated object as the
    ``result`` field; we accept either shape for forward-compat.
    """
    raw_result = response.raw.get("result") if isinstance(response.raw, dict) else None
    if isinstance(raw_result, dict):
        return raw_result
    text = (response.text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LlmTransportError(f"reviewer/judge returned non-JSON: {text[:200]}") from e


# ---------------------------------------------------------------------------
# Verdict application
# ---------------------------------------------------------------------------


def _apply_verdicts_and_audit(
    engine: Any,
    *,
    verdicts: List[ScoutVerdict],
    candidates_by_symbol: Dict[str, IntelCandidateCrypto],
    dismiss_ttl_hours: int,
    prompt_version: str,
    now: dt.datetime,
) -> tuple[int, int, int]:
    """Update intel_candidates_crypto rows + persist scout_debate_runs_crypto rows.

    Returns (n_elevated, n_dismissed, n_audit_rows_written).
    """
    elevated = 0
    dismissed = 0
    audit = 0
    dismiss_until = now + dt.timedelta(hours=dismiss_ttl_hours)

    with Session(engine) as session:
        for v in verdicts:
            if v.verdict not in ("elevate", "dismiss"):
                continue
            cand = candidates_by_symbol.get(v.symbol)
            if cand is None:
                logger.warning("scout judge produced verdict for unknown symbol %s", v.symbol)
                continue

            stmt = (
                sa_update(IntelCandidateCrypto)
                .where(IntelCandidateCrypto.symbol == v.symbol)
                .values(
                    scout_verdict=v.verdict,
                    scout_dismissed_until=(dismiss_until if v.verdict == "dismiss" else None),
                )
            )
            session.execute(stmt)
            if v.verdict == "elevate":
                elevated += 1
            else:
                dismissed += 1

            session.add(ScoutDebateRunCrypto(
                run_at=now,
                symbol=v.symbol,
                candidate_score=float(cand.score) if cand.score is not None else None,
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
    dismiss_ttl_hours: int = 12,
    transport: Any = None,
    now: Optional[dt.datetime] = None,
    lessons_block: Optional[str] = None,
) -> ScoutRunResult:
    """Run one crypto scout-debate tick. Sequential per ADR 0003.

    ``transport`` is injected for tests; production wires through the
    ``shared.llm_transport`` Claude-CLI subprocess transport.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    candidates = select_candidates(
        engine, threshold=threshold, batch_limit=batch_limit, now=now,
    )
    if not candidates:
        return ScoutRunResult(debated=0, elevated=0, dismissed=0, skipped=0)

    # Phase 1D — pull the freshest lesson block when the caller didn't
    # override. Fail-soft: if the lesson loop blew up, fall back to the
    # placeholder rather than refuse to debate.
    if lessons_block is None:
        try:
            from trading_bot.pipelines.crypto.lesson_loop import latest_lesson_block
            lessons_block = latest_lesson_block(engine, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning("scout_debate lesson injection failed: %s", e)
            lessons_block = "(lesson injection failed; debate proceeding without lessons)"

    candidates_by_symbol = {c.symbol: c for c in candidates}

    transport_skeptic = transport or get_transport(role_name="scout_skeptic", engine=engine)
    transport_judge = transport or get_transport(role_name="scout_judge", engine=engine)

    # ---- Call 1: combined skeptic + analyst (Sonnet) -----------------
    try:
        briefs = _run_reviewer_call(
            transport_skeptic,
            candidates=candidates,
            lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("scout_debate skipped: rate-limited (%s)", e)
        return ScoutRunResult(
            debated=len(candidates), elevated=0, dismissed=0,
            skipped=len(candidates), error="rate_limited",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("scout_debate reviewer call failed: %s", e)
        return ScoutRunResult(
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
        logger.warning("scout_debate judge skipped: rate-limited (%s)", e)
        return ScoutRunResult(
            debated=len(candidates), elevated=0, dismissed=0,
            skipped=len(candidates), error="rate_limited_judge",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("scout_debate judge call failed: %s", e)
        return ScoutRunResult(
            debated=len(candidates), elevated=0, dismissed=0,
            skipped=len(candidates), error=f"judge_error:{e}",
        )

    # ---- Apply verdicts ----------------------------------------------
    verdicts = [
        ScoutVerdict(
            symbol=v.get("symbol", ""),
            verdict=v.get("verdict", ""),
            confidence=v.get("confidence", "low"),
            reason=v.get("reason", ""),
            skeptic_text=skeptic_briefs.get(v.get("symbol", ""), ""),
            analyst_text=analyst_briefs.get(v.get("symbol", ""), ""),
        )
        for v in verdicts_raw
    ]

    judge_persona_obj = parse(judge_persona_mod.PERSONA)
    skeptic_persona_obj = parse(skeptic_persona_mod.PERSONA)
    analyst_persona_obj = parse(analyst_persona_mod.PERSONA)
    prompt_version = (
        f"crypto_scout/skeptic={skeptic_persona_obj.prompt_version}"
        f",analyst={analyst_persona_obj.prompt_version}"
        f",judge={judge_persona_obj.prompt_version}"
    )

    elevated, dismissed, _ = _apply_verdicts_and_audit(
        engine,
        verdicts=verdicts,
        candidates_by_symbol=candidates_by_symbol,
        dismiss_ttl_hours=dismiss_ttl_hours,
        prompt_version=prompt_version,
        now=now,
    )

    return ScoutRunResult(
        debated=len(candidates),
        elevated=elevated,
        dismissed=dismissed,
        skipped=len(candidates) - elevated - dismissed,
        verdicts=verdicts,
    )


# ---------------------------------------------------------------------------
# Phase 1B.4 — whale_alert dismissal override
# ---------------------------------------------------------------------------


def override_dismissals_for_whale_alert(
    engine: Any,
    *,
    now: Optional[dt.datetime] = None,
    lookback_hours: int = 6,
) -> int:
    """Re-elevate dismissed crypto symbols when fresh whale_alert events arrive.

    Mirrors the stocks-side ``override_dismissals_for_sec_8k`` pattern.
    A confirmed >$1M on-chain transfer is the crypto equivalent of an
    SEC 8-K — it should override a prior scout-debate dismissal because
    the new primary-source event materially changes the thesis.

    Returns the number of candidates whose dismissal was cleared.
    """
    from trading_bot.pipelines.crypto.state_db import IntelEventCrypto

    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=lookback_hours)

    with Session(engine) as session:
        recent_whales = (
            session.query(IntelEventCrypto.symbol)
            .filter(
                IntelEventCrypto.source == "whale_alert",
                IntelEventCrypto.ingested_at >= cutoff,
            )
            .distinct()
            .all()
        )
        whale_symbols = {row[0] for row in recent_whales}
        if not whale_symbols:
            return 0

        result = session.execute(
            sa_update(IntelCandidateCrypto)
            .where(
                IntelCandidateCrypto.symbol.in_(whale_symbols),
                IntelCandidateCrypto.scout_dismissed_until.isnot(None),
                IntelCandidateCrypto.scout_dismissed_until > now,
            )
            .values(
                scout_dismissed_until=None,
                scout_verdict=None,  # null → eligible for fresh debate
            )
        )
        session.commit()
        return result.rowcount or 0
