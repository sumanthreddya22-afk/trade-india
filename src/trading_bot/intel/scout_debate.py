"""Phase B — Scout Debate (Debate #1).

Sequential 3-call LLM committee that fires after the intel ingestor's
``aggregator.roll_up()`` for any tick that surfaced new high-score
candidates. ONE debate per tick, batched across all candidates above
threshold (~48 debates/day vs ~1000 if per-symbol).

Personas (versioned in ``trading_bot.personas/``):
  1. Skeptic  — Forensic short-seller (looks for reasons to dismiss)
  2. Analyst  — Sell-side equity analyst (makes the case to elevate)
  3. Judge    — Director of Equity Research (final per-symbol verdict)

Verdicts (per symbol):
  ``elevate`` → multiply ``IntelCandidate.score`` by ``elevate_boost``
  ``dismiss`` → set ``scout_dismissed_until = now + ttl_hours`` (filtered
               by the pool reader; auto re-debatable after TTL expires)

Fail-SOFT contract: any error path (no creds, budget halt, SDK exception,
judge schema mismatch, no candidates) returns an empty
``ScoutDebateResult`` — no boosts, no dismissals applied. Caller MUST
treat fail-soft as "leave candidates as they are" (NOT "dismiss them all").

This is the inverse of entry/hold debate fail-soft: there, fail-soft
skips the trade (defensive). Here, fail-soft preserves the candidate
(also defensive — refusing to dismiss a real signal because the LLM
gate is unreachable).

SEQUENTIAL EXECUTION GUARANTEE: skeptic → analyst → judge are three
back-to-back LLM calls. The analyst reads the skeptic's verbatim text;
the judge reads both. No parallelism inside the debate.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trading_bot.anthropic_client import (
    AnthropicCredsMissingError,
    BudgetExceededError,
    default_architect_model,
)
from trading_bot.mailbox_backed_client import (
    MailboxBackedClient,
    MailboxRouting,
)
from trading_bot.personas import scout_skeptic, scout_analyst, scout_judge
from trading_bot.state_db import IntelCandidate, ScoutDebateRun


log = logging.getLogger(__name__)


# Default knobs — overridable via strategy/config.yaml::intel section.
DEFAULT_THRESHOLD = 3.0          # min candidate score to include in brief
DEFAULT_BATCH_LIMIT = 8          # max candidates per debate (cap LLM tokens)
DEFAULT_DAILY_CAP = 48           # max scout debates per day
DEFAULT_ELEVATE_BOOST = 1.5      # score multiplier on elevate
DEFAULT_DISMISS_TTL_HOURS = 24   # how long a dismissed symbol stays hidden


# ---------------------------------------------------------------------------
# Verdict types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoutVerdict:
    """Per-symbol verdict from the scout judge."""
    symbol: str
    verdict: Literal["elevate", "dismiss"]
    confidence: Literal["high", "medium", "low"]
    reason: str


@dataclass
class ScoutDebateResult:
    """Composite result of one scout-debate tick."""
    verdicts: list[ScoutVerdict] = field(default_factory=list)
    skeptic_text: str = ""
    analyst_text: str = ""
    n_candidates_in_brief: int = 0
    skipped_reason: str = ""    # fail-soft: populated on no-op


class _PerSymbolJudgeOutput(BaseModel):
    symbol: str = Field(description="REQUIRED. Ticker symbol verbatim from the brief.")
    verdict: Literal["elevate", "dismiss"] = Field(
        description=(
            "REQUIRED. 'elevate' to boost the candidate score; 'dismiss' "
            "to filter the symbol from the pool for the dismiss-TTL window."
        ),
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="REQUIRED. 'high'|'medium'|'low' confidence in the verdict.",
    )
    reason: str = Field(
        default="",
        max_length=2000,
        description=(
            "REQUIRED. 1-2 sentences citing the load-bearing fact "
            "(specific source, pump signature, cross-source confirmation). "
            "Audit trail depends on this — do not omit."
        ),
    )


class _ScoutJudgeOutput(BaseModel):
    verdicts: list[_PerSymbolJudgeOutput] = Field(
        description=(
            "REQUIRED. One entry PER candidate symbol in the brief, in the "
            "same order. Do not omit any symbol."
        ),
    )


_JUDGE_TOOL_SCHEMA = _ScoutJudgeOutput.model_json_schema()


# ---------------------------------------------------------------------------
# Predicate + counters
# ---------------------------------------------------------------------------


def should_scout_debate(
    *,
    daily_debate_count: int,
    daily_cap: int = DEFAULT_DAILY_CAP,
) -> bool:
    """Predicate: only fire scout debate when we're under the daily cap."""
    if daily_cap <= 0:
        return False
    return daily_debate_count < daily_cap


def count_todays_scout_debates(engine) -> int:
    """Today's row count from ``scout_debate_runs``. Defensive on errors —
    returns 0 so the gate fail-opens (rather than skipping every tick when
    the audit table is missing)."""
    from sqlalchemy import func, select
    try:
        today_start = dt.datetime.combine(
            dt.date.today(), dt.time.min, tzinfo=dt.timezone.utc,
        )
        with Session(engine) as s:
            count = s.execute(
                select(func.count(ScoutDebateRun.id))
                .where(ScoutDebateRun.run_at >= today_start)
            ).scalar_one()
        return int(count or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def _new_candidates_for_debate(
    engine,
    *,
    threshold: float,
    batch_limit: int,
    asset_class: str = "stock",
    now: dt.datetime | None = None,
) -> list[IntelCandidate]:
    """Pull candidates eligible for THIS tick's scout debate.

    Eligibility:
      - asset_class match
      - score >= threshold
      - scout_verdict IS NULL (not yet debated, or dismissal TTL recently
        expired and operator wiped scout_dismissed_until)

    Ordered by score desc, capped at ``batch_limit``.
    """
    from sqlalchemy import desc as _desc
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(IntelCandidate)
            .filter(IntelCandidate.asset_class == asset_class)
            .filter(IntelCandidate.score >= threshold)
            .filter(IntelCandidate.scout_verdict.is_(None))
            .order_by(_desc(IntelCandidate.score))
            .limit(batch_limit)
            .all()
        )
        # Detach so caller can read attrs after session closes
        for r in rows:
            session.expunge(r)
    return rows


def _candidate_brief_line(c: IntelCandidate) -> str:
    """One brief-friendly line per candidate. Compact but complete."""
    import json as _json
    try:
        sources_payload = _json.loads(c.sources_json or "{}")
        sources_str = ", ".join(
            f"{k}({v})" for k, v in sorted(sources_payload.items())
        )
    except Exception:
        sources_str = "(parse error)"
    sent = (
        f"{float(c.sentiment_avg):+.2f}"
        if c.sentiment_avg is not None else "(none)"
    )
    return (
        f"  {c.symbol}: score={float(c.score):.2f}, mentions={c.n_mentions}, "
        f"n_sources={c.n_sources}, sentiment_avg={sent}, "
        f"sources=[{sources_str}], top_reason={(c.top_reason or '')[:200]}"
    )


def _build_brief(candidates: list[IntelCandidate], lesson_block: str = "") -> str:
    if not candidates:
        return "(no candidates)"
    lines = [
        f"Scout debate batch — {len(candidates)} new high-score candidate(s):",
        "",
    ]
    for c in candidates:
        lines.append(_candidate_brief_line(c))
    lines.append("")
    if lesson_block:
        lines.append("RECENT LESSONS (read this before voting):")
        lines.append(lesson_block)
        lines.append("")
    lines.append(
        "Per-symbol output: include a verdict for EVERY symbol above, "
        "in the same order. No omissions."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply verdicts to intel_candidates
# ---------------------------------------------------------------------------


def apply_verdicts(
    engine,
    *,
    verdicts: list[ScoutVerdict],
    asset_class: str = "stock",
    elevate_boost: float = DEFAULT_ELEVATE_BOOST,
    dismiss_ttl_hours: float = DEFAULT_DISMISS_TTL_HOURS,
    now: dt.datetime | None = None,
) -> dict:
    """Mutate ``intel_candidates`` per verdicts. Returns counts.

    Sequential per verdict — no concurrent writes. Uses a single Session
    transaction so a partial failure leaves the table consistent.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    dismiss_until = now + dt.timedelta(hours=dismiss_ttl_hours)
    n_elevated = 0
    n_dismissed = 0
    n_missing = 0
    with Session(engine) as session:
        for v in verdicts:
            row = (
                session.query(IntelCandidate)
                .filter(IntelCandidate.symbol == v.symbol)
                .filter(IntelCandidate.asset_class == asset_class)
                .first()
            )
            if row is None:
                n_missing += 1
                continue
            row.scout_verdict = v.verdict
            if v.verdict == "elevate":
                row.score = round(float(row.score) * float(elevate_boost), 4)
                row.scout_dismissed_until = None
                n_elevated += 1
            elif v.verdict == "dismiss":
                row.scout_dismissed_until = dismiss_until
                n_dismissed += 1
        session.commit()
    return {
        "elevated": n_elevated,
        "dismissed": n_dismissed,
        "missing_rows": n_missing,
    }


def write_audit_rows(
    engine,
    *,
    verdicts: list[ScoutVerdict],
    candidates: list[IntelCandidate],
    skeptic_text: str,
    analyst_text: str,
    asset_class: str = "stock",
    prompt_version: str = "",
    now: dt.datetime | None = None,
) -> int:
    """Persist one row per (symbol, verdict) into ``scout_debate_runs``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cand_by_sym = {c.symbol.upper(): c for c in candidates}
    n = 0
    with Session(engine) as session:
        for v in verdicts:
            c = cand_by_sym.get(v.symbol.upper())
            session.add(ScoutDebateRun(
                run_at=now,
                asset_class=asset_class,
                symbol=v.symbol,
                candidate_score=float(c.score) if c is not None else None,
                top_reason=(c.top_reason if c is not None else "") or "",
                verdict=v.verdict,
                confidence=v.confidence,
                judge_reason=v.reason or "",
                skeptic_text=skeptic_text or "",
                analyst_text=analyst_text or "",
                prompt_version=prompt_version,
            ))
            n += 1
        session.commit()
    return n


# ---------------------------------------------------------------------------
# Sequential 3-call debate
# ---------------------------------------------------------------------------


def _persona_version() -> str:
    """Compose a single version tag from the three persona modules.

    Bumps when ANY persona's VERSION changes — analysis can attribute
    verdicts to specific persona-prompt versions.
    """
    return (
        f"skeptic={scout_skeptic.VERSION}"
        f"|analyst={scout_analyst.VERSION}"
        f"|judge={scout_judge.VERSION}"
    )


def run_scout_debate(
    engine,
    *,
    asset_class: str = "stock",
    threshold: float = DEFAULT_THRESHOLD,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
    elevate_boost: float = DEFAULT_ELEVATE_BOOST,
    dismiss_ttl_hours: float = DEFAULT_DISMISS_TTL_HOURS,
    role_name: str = "scout_debate",
    max_turn_tokens: int = 800,
    max_judge_tokens: int = 1200,
    use_mailbox: bool = True,
    mailbox_timeout_seconds: float = 600.0,
    now: dt.datetime | None = None,
) -> ScoutDebateResult:
    """Sequential 3-call scout debate. Returns the verdicts AND applies
    them to ``intel_candidates`` + writes audit rows.

    Fail-soft: any error returns ``ScoutDebateResult`` with empty verdicts
    and a populated ``skipped_reason``. Pool state is left untouched.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    candidates = _new_candidates_for_debate(
        engine,
        threshold=threshold, batch_limit=batch_limit,
        asset_class=asset_class, now=now,
    )
    if not candidates:
        return ScoutDebateResult(skipped_reason="no new high-score candidates")

    # Phase D — pull the latest lesson block (if any) and inject under
    # "RECENT LESSONS" in the brief.
    try:
        from trading_bot.lesson_loop import latest_lesson_block
        lesson_block = latest_lesson_block(engine)
    except Exception:
        lesson_block = ""

    try:
        client = MailboxBackedClient(
            role_name=role_name,
            model=default_architect_model(),
            engine=engine,
            routing=MailboxRouting(
                enabled=use_mailbox,
                timeout_seconds=mailbox_timeout_seconds,
                model_class="judge",
            ),
        )
    except AnthropicCredsMissingError:
        log.info("scout_debate: skipped (no anthropic creds) — fail soft")
        return ScoutDebateResult(
            n_candidates_in_brief=len(candidates),
            skipped_reason="no anthropic creds",
        )

    brief = _build_brief(candidates, lesson_block=lesson_block)

    # SEQUENTIAL: skeptic → analyst → judge. Each step waits for prior.
    try:
        skeptic = client.complete(
            system=scout_skeptic.PROMPT,
            messages=[{"role": "user", "content": brief}],
            max_tokens=max_turn_tokens,
        )
        analyst_user = (
            f"{brief}\n\nSKEPTIC'S BRIEF (forensic short-seller, looks for "
            f"reasons NOT to elevate):\n{skeptic.text}\n"
        )
        analyst = client.complete(
            system=scout_analyst.PROMPT,
            messages=[{"role": "user", "content": analyst_user}],
            max_tokens=max_turn_tokens,
        )
        judge_user = (
            f"{brief}\n\nSKEPTIC (forensic short-seller):\n{skeptic.text}\n\n"
            f"ANALYST (sell-side equity analyst):\n{analyst.text}\n"
        )
        judge = client.complete_structured(
            system=scout_judge.PROMPT,
            messages=[{"role": "user", "content": judge_user}],
            tool_name="cast_scout_verdict",
            tool_description=(
                "Cast per-symbol scout verdicts. ONE verdict per candidate "
                "in the brief, in the same order. 'elevate' boosts score; "
                "'dismiss' filters from pool for TTL window."
            ),
            tool_schema=_JUDGE_TOOL_SCHEMA,
            max_tokens=max_judge_tokens,
        )
    except BudgetExceededError:
        log.info("scout_debate: skipped (budget halt) — fail soft")
        return ScoutDebateResult(
            n_candidates_in_brief=len(candidates),
            skipped_reason="budget halt",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("scout_debate: SDK error, failing soft: %s", e)
        return ScoutDebateResult(
            n_candidates_in_brief=len(candidates),
            skipped_reason=f"sdk error: {e}",
        )

    if not (judge.used_structured and judge.data):
        log.warning("scout_debate: judge returned free text only, failing soft")
        return ScoutDebateResult(
            n_candidates_in_brief=len(candidates),
            skipped_reason="judge unstructured",
            skeptic_text=skeptic.text,
            analyst_text=analyst.text,
        )
    try:
        parsed = _ScoutJudgeOutput.model_validate(judge.data)
    except Exception as e:  # noqa: BLE001
        log.warning("scout_debate: judge schema mismatch, failing soft: %s", e)
        return ScoutDebateResult(
            n_candidates_in_brief=len(candidates),
            skipped_reason=f"judge schema mismatch: {e}",
            skeptic_text=skeptic.text,
            analyst_text=analyst.text,
        )

    # Validate: every candidate should have a verdict; drop verdicts for
    # symbols not in the brief (judge hallucination); fail-soft if no
    # overlap at all.
    brief_symbols = {c.symbol.upper() for c in candidates}
    verdicts: list[ScoutVerdict] = []
    for v in parsed.verdicts:
        sym = (v.symbol or "").upper().strip()
        if not sym or sym not in brief_symbols:
            log.info(
                "scout_debate: judge returned verdict for %s not in brief — dropped",
                sym,
            )
            continue
        verdicts.append(ScoutVerdict(
            symbol=sym, verdict=v.verdict, confidence=v.confidence,
            reason=(v.reason or "").strip(),
        ))
    if not verdicts:
        log.warning("scout_debate: judge returned no usable verdicts, failing soft")
        return ScoutDebateResult(
            n_candidates_in_brief=len(candidates),
            skipped_reason="no usable verdicts",
            skeptic_text=skeptic.text,
            analyst_text=analyst.text,
        )

    # Apply (mutate intel_candidates) + audit (write scout_debate_runs).
    apply_verdicts(
        engine, verdicts=verdicts, asset_class=asset_class,
        elevate_boost=elevate_boost, dismiss_ttl_hours=dismiss_ttl_hours,
        now=now,
    )
    write_audit_rows(
        engine, verdicts=verdicts, candidates=candidates,
        skeptic_text=skeptic.text, analyst_text=analyst.text,
        asset_class=asset_class, prompt_version=_persona_version(), now=now,
    )

    return ScoutDebateResult(
        verdicts=verdicts,
        skeptic_text=skeptic.text,
        analyst_text=analyst.text,
        n_candidates_in_brief=len(candidates),
    )


# ---------------------------------------------------------------------------
# SEC 8-K override path
# ---------------------------------------------------------------------------


def override_dismissals_for_sec_8k(
    engine,
    *,
    lookback_minutes: int = 60,
    asset_class: str = "stock",
    now: dt.datetime | None = None,
) -> dict:
    """Re-elevate dismissed symbols when a fresh sec_8k event arrives.

    Plan rule: a `dismiss` is short-circuited if a higher-tier source
    (`sec_8k`) confirms the symbol within the dismissal window. Prevents
    lessons-driven false negatives.

    Implementation: scan recent ``intel_events`` for sec_8k entries within
    ``lookback_minutes``; for each affected symbol, clear
    ``scout_dismissed_until`` so the pool reader surfaces it again. Does
    NOT bump the score — the next roll_up tick handles that with the
    fresh event already in the pool.
    """
    from trading_bot.state_db import IntelEvent
    from sqlalchemy import desc as _desc
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(minutes=lookback_minutes)
    overrode: list[str] = []
    with Session(engine) as session:
        # Get distinct symbols with fresh sec_8k events
        recent_syms = {
            row.symbol for row in (
                session.query(IntelEvent)
                .filter(IntelEvent.source == "sec_8k")
                .filter(IntelEvent.ingested_at >= cutoff)
                .order_by(_desc(IntelEvent.ingested_at))
                .all()
            )
        }
        for sym in recent_syms:
            row = (
                session.query(IntelCandidate)
                .filter(IntelCandidate.symbol == sym)
                .filter(IntelCandidate.asset_class == asset_class)
                .first()
            )
            if row is None:
                continue
            if row.scout_dismissed_until is None:
                continue
            # Only override if dismissal is still active
            til = row.scout_dismissed_until
            if til.tzinfo is None:
                til = til.replace(tzinfo=dt.timezone.utc)
            if til < now:
                continue
            row.scout_dismissed_until = None
            row.scout_verdict = None  # re-debatable next tick
            overrode.append(sym)
        session.commit()
    return {"overrode": overrode, "n_overrode": len(overrode)}
