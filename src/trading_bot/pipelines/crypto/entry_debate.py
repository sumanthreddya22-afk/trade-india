"""Crypto entry debate (Phase 1G follow-on) — two-call structure.

Per-pipeline replacement for the shared ``trading_bot.entry_debate``
when called for crypto candidates. Same two-call shape as the scout +
hold debates:

  Call 1 (Sonnet 4.6) — Combined Aggressive + Conservative + Neutral
    Kai Tanaka writes per-candidate aggressive briefs.
    Anya Volk reads Kai's text in the same context window and writes
    per-candidate conservative briefs.
    Rohan Mehta reads both prior reviewers' briefs and writes the
    asymmetry-lens neutral briefs.
    Single structured-JSON return.

  Call 2 (Opus 4.7) — Judge
    Diane Pereira reads all three reviewer outputs verbatim and
    produces the audit-of-record verdict (place / skip / defer_restale)
    plus an ``adjusted_qty`` field for sizing decisions.

Verdict application is callback-based — this module computes the
verdict and persists the audit row, but the caller plugs in the
broker action via ``execute_order`` (typically routes through the
shared submit_txn).

Failure mode: any exception in either LLM call → SkipVerdict
('rate_limited' or generic error). Caller must NOT submit an order
on a non-place verdict.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.personas import (
    entry_aggressive as aggressive_persona_mod,
    entry_conservative as conservative_persona_mod,
    entry_judge as judge_persona_mod,
    entry_neutral as neutral_persona_mod,
)
from trading_bot.pipelines.crypto.state_db import EntryDebateRunCrypto
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
class EntryCandidate:
    """One candidate up for entry debate, with its proposed order shape."""
    symbol: str
    candidate_score: Optional[float] = None
    intel_top_reason: str = ""
    sentiment_avg: Optional[float] = None
    side: str = "buy"
    proposed_qty: float = 0.0
    proposed_entry_price: float = 0.0
    proposed_stop_price: float = 0.0
    proposed_target_price: float = 0.0


@dataclass
class EntryVerdict:
    symbol: str
    verdict: str            # place | skip | defer_restale
    confidence: str
    reason: str
    adjusted_qty: Optional[float] = None
    aggressive_text: str = ""
    conservative_text: str = ""
    neutral_text: str = ""


@dataclass
class EntryRunResult:
    debated: int
    placed: int
    skipped: int
    deferred: int
    verdicts: List[EntryVerdict] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Brief composition
# ---------------------------------------------------------------------------


def _order_block(candidates: Sequence[EntryCandidate]) -> str:
    if not candidates:
        return "  (no candidates)"
    return "\n".join(
        f"  - {c.symbol} | side={c.side} qty={c.proposed_qty} "
        f"entry={c.proposed_entry_price:.4f} stop={c.proposed_stop_price:.4f} "
        f"target={c.proposed_target_price:.4f}"
        for c in candidates
    )


def _intel_block(candidates: Sequence[EntryCandidate]) -> str:
    if not candidates:
        return "  (no candidates)"
    out: List[str] = []
    for c in candidates:
        sentiment = (
            f"{c.sentiment_avg:+.2f}" if c.sentiment_avg is not None else "n/a"
        )
        out.append(
            f"  - {c.symbol} | score={c.candidate_score} | sentiment_avg={sentiment}\n"
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
                    "symbol":     {"type": "string"},
                    "verdict":    {"type": "string", "enum": ["place", "skip", "defer_restale"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason":     {"type": "string"},
                    "adjusted_qty": {"type": ["number", "null"]},
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
    candidates: Sequence[EntryCandidate],
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
        "You are running a three-persona crypto entry debate.\n\n"
        "STEP 1 — Act as Kai Tanaka:\n"
        f"{aggressive_prompt}\n\n"
        "STEP 2 — Then in the SAME response, act as Anya Volk reading "
        "Kai's briefs verbatim from above:\n"
        f"{conservative_prompt}\n\n"
        "STEP 3 — Then in the SAME response, act as Rohan Mehta reading "
        "BOTH Kai's and Anya's briefs verbatim from above:\n"
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
    candidates: Sequence[EntryCandidate],
    aggressive_briefs: Dict[str, str],
    conservative_briefs: Dict[str, str],
    neutral_briefs: Dict[str, str],
    regime: str,
    lessons_block: str,
) -> List[Dict[str, Any]]:
    judge = parse(judge_persona_mod.PERSONA)
    order_block = _order_block(candidates)

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
        raise LlmTransportError(f"entry reviewer/judge returned non-JSON: {text[:200]}") from e


# ---------------------------------------------------------------------------
# Audit + execution plumbing
# ---------------------------------------------------------------------------


def _persist_audit(
    engine: Any,
    *,
    verdict: EntryVerdict,
    candidate: EntryCandidate,
    regime: str,
    entry_order_id: Optional[str],
    prompt_version: str,
    now: dt.datetime,
) -> None:
    with Session(engine) as session:
        session.add(EntryDebateRunCrypto(
            run_at=now,
            symbol=verdict.symbol,
            candidate_score=candidate.candidate_score,
            intel_top_reason=candidate.intel_top_reason,
            sentiment_avg=candidate.sentiment_avg,
            regime=regime,
            proposed_qty=candidate.proposed_qty,
            proposed_entry_price=candidate.proposed_entry_price,
            proposed_stop_price=candidate.proposed_stop_price,
            proposed_target_price=candidate.proposed_target_price,
            verdict=verdict.verdict,
            confidence=verdict.confidence,
            adjusted_qty=verdict.adjusted_qty,
            judge_reason=verdict.reason,
            aggressive_text=verdict.aggressive_text,
            conservative_text=verdict.conservative_text,
            neutral_text=verdict.neutral_text,
            entry_order_id=entry_order_id,
            prompt_version=prompt_version,
            synthetic=False,
        ))
        session.commit()


# ---------------------------------------------------------------------------
# Order executor signature
# ---------------------------------------------------------------------------


class EntryOrderExecutor:
    """Pluggable broker call. Production wires through shared.submit_txn."""

    def submit_entry(
        self, *, candidate: EntryCandidate, adjusted_qty: float,
    ) -> Optional[str]:
        """Returns the broker order id on success, None on rejection."""
        ...


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_entry_debate(
    engine: Any,
    *,
    candidates: Sequence[EntryCandidate],
    regime: str = "crypto_range",
    executor: Optional[EntryOrderExecutor] = None,
    transport: Any = None,
    now: Optional[dt.datetime] = None,
    lessons_block: Optional[str] = None,
) -> EntryRunResult:
    """Run one crypto entry-debate tick over the supplied candidates.

    ``executor`` is None-safe: when omitted, verdicts are computed +
    audited but no orders are submitted (useful for dry-run / unit
    tests). Production callers pass a wrapper that delegates to
    ``shared.alpaca_client.place_order_with_stop_loss`` via the
    optimistic-concurrency submit_txn.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if not candidates:
        return EntryRunResult(debated=0, placed=0, skipped=0, deferred=0)

    candidates = list(candidates)
    candidates_by_symbol = {c.symbol: c for c in candidates}

    # Phase 1D — pull the freshest lesson block when caller didn't override
    if lessons_block is None:
        try:
            from trading_bot.pipelines.crypto.lesson_loop import latest_lesson_block
            lessons_block = latest_lesson_block(engine, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning("entry_debate lesson injection failed: %s", e)
            lessons_block = "(lesson injection failed; debate proceeding without lessons)"

    transport_reviewer = transport or get_transport(role_name="entry_aggressive", engine=engine)
    transport_judge = transport or get_transport(role_name="entry_judge", engine=engine)

    # ---- Call 1: combined three reviewers (Sonnet) -------------------
    try:
        briefs = _run_reviewer_call(
            transport_reviewer, candidates=candidates, regime=regime,
            lessons_block=lessons_block,
        )
    except SubscriptionRateLimited as e:
        logger.warning("entry_debate skipped: rate-limited (%s)", e)
        return EntryRunResult(
            debated=len(candidates), placed=0, skipped=0,
            deferred=len(candidates), error="rate_limited",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("entry_debate reviewer call failed")
        return EntryRunResult(
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
        logger.warning("entry_debate judge skipped: rate-limited (%s)", e)
        return EntryRunResult(
            debated=len(candidates), placed=0, skipped=0,
            deferred=len(candidates), error="rate_limited_judge",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("entry_debate judge call failed")
        return EntryRunResult(
            debated=len(candidates), placed=0, skipped=0,
            deferred=len(candidates), error=f"judge_error:{e}",
        )

    # ---- Build verdicts + apply ---------------------------------------
    aggressive_obj = parse(aggressive_persona_mod.PERSONA)
    conservative_obj = parse(conservative_persona_mod.PERSONA)
    neutral_obj = parse(neutral_persona_mod.PERSONA)
    judge_obj = parse(judge_persona_mod.PERSONA)
    prompt_version = (
        f"crypto_entry/aggressive={aggressive_obj.prompt_version}"
        f",conservative={conservative_obj.prompt_version}"
        f",neutral={neutral_obj.prompt_version}"
        f",judge={judge_obj.prompt_version}"
    )

    placed = skipped = deferred = 0
    out_verdicts: List[EntryVerdict] = []

    for v in verdicts_raw:
        symbol = v.get("symbol", "")
        cand = candidates_by_symbol.get(symbol)
        if cand is None:
            logger.warning("entry judge produced verdict for unknown symbol %s", symbol)
            continue

        verdict_str = v.get("verdict", "skip")
        adjusted_qty = v.get("adjusted_qty")
        if adjusted_qty is None and verdict_str == "place":
            adjusted_qty = cand.proposed_qty

        verdict = EntryVerdict(
            symbol=symbol, verdict=verdict_str,
            confidence=v.get("confidence", "low"),
            reason=v.get("reason", ""),
            adjusted_qty=adjusted_qty,
            aggressive_text=aggressive_briefs.get(symbol, ""),
            conservative_text=conservative_briefs.get(symbol, ""),
            neutral_text=neutral_briefs.get(symbol, ""),
        )
        out_verdicts.append(verdict)

        entry_order_id: Optional[str] = None
        if executor is not None and verdict_str == "place" and adjusted_qty is not None:
            try:
                entry_order_id = executor.submit_entry(
                    candidate=cand, adjusted_qty=float(adjusted_qty),
                )
            except Exception as e:  # noqa: BLE001 — broker error never crashes audit
                logger.exception("entry submit failed for %s: %s", symbol, e)
                entry_order_id = None

        _persist_audit(
            engine, verdict=verdict, candidate=cand, regime=regime,
            entry_order_id=entry_order_id, prompt_version=prompt_version, now=now,
        )

        if verdict_str == "place":
            placed += 1
        elif verdict_str == "skip":
            skipped += 1
        elif verdict_str == "defer_restale":
            deferred += 1

    return EntryRunResult(
        debated=len(candidates),
        placed=placed, skipped=skipped, deferred=deferred,
        verdicts=out_verdicts,
    )
