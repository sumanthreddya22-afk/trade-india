"""Phase D — Lesson loop: outcome aggregation + brief injection.

Two responsibilities:

  1. ``aggregate_outcomes`` reads the last N days of debate runs joined
     with closed-trade outcomes and produces a structured report:
       - per-verdict winrates (place / skip / exit_now / tighten / hold)
       - per-source attribution (winning trades' contributing sources)
       - sample losing trades with judge_reason text
       - shadow-tracked skipped trades (false negatives)
     Returns a dict the analyzer LLM consumes as user-message context.

  2. ``write_lesson`` persists the analyzer's output to the
     ``debate_lessons`` table so future debate briefs can read it via
     ``latest_lesson_block``.

  3. ``latest_lesson_block`` returns the most-recent lesson summary
     formatted as a multi-line string ready for inclusion in a debate
     brief under "RECENT LESSONS". Returns "" when no lesson exists or
     the most recent one is too stale (>7 days by default).

Sequential execution preserved: aggregation is one SQL query per source
table, run sequentially (no concurrent DB sessions).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import desc as _desc
from sqlalchemy.orm import Session

from trading_bot.state_db import (
    DebateLesson, EntryDebateRun, HoldDebateRun,
    UnblockDebateRun, ScoutDebateRun,
)


log = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_LESSON_FRESHNESS_DAYS = 7


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class OutcomeReport:
    """Structured aggregation of recent debate outcomes. Fed into the
    Performance Attribution Analyst persona as the user-message context.
    """
    lookback_days: int
    n_trades_closed: int = 0
    n_entry_debates: int = 0
    n_unblock_debates: int = 0
    n_hold_debates: int = 0
    overall_place_winrate: float | None = None
    overall_skip_winrate: float | None = None
    per_verdict_winrate: dict[str, dict] = field(default_factory=dict)
    per_source_winrate: dict[str, dict] = field(default_factory=dict)
    losing_patterns: list[dict] = field(default_factory=list)
    shadow_skips: list[dict] = field(default_factory=list)


def aggregate_outcomes(
    engine,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: dt.datetime | None = None,
    sample_losing: int = 8,
    sample_skips: int = 8,
) -> OutcomeReport:
    """Join debate runs with closed-trade outcomes (closed_pnl_pct
    backfilled by reconciler) and aggregate into an OutcomeReport.

    Sequential reads: entry_debate_runs, unblock_debate_runs, hold_debate_runs
    are queried one at a time. No concurrent DB sessions.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=lookback_days)
    report = OutcomeReport(lookback_days=lookback_days)

    # Per-verdict winrate accumulators
    verdict_acc: dict[str, list[float]] = {}     # verdict -> list of pnl_pct
    source_acc: dict[str, list[float]] = {}      # source  -> list of pnl_pct
    losing: list[dict] = []
    shadow_candidates: list[dict] = []

    with Session(engine) as session:
        entry_rows = (
            session.query(EntryDebateRun)
            .filter(EntryDebateRun.run_at >= cutoff)
            .all()
        )
    report.n_entry_debates = len(entry_rows)
    for r in entry_rows:
        if r.closed_pnl_pct is None:
            # Skipped or fail-soft → potentially a shadow candidate
            if r.verdict == "skip":
                shadow_candidates.append({
                    "symbol": r.symbol,
                    "judge_reason": r.judge_reason or "",
                    "verdict": r.verdict,
                    "intel_score": r.intel_score,
                    "regime": r.regime,
                    "run_at": r.run_at.isoformat() if r.run_at else "",
                })
            continue
        verdict_acc.setdefault(r.verdict, []).append(float(r.closed_pnl_pct))
        # Per-source attribution: signal_reason often references sources
        # (e.g., "sec_8k catalyst"); rough but the lesson loop is heuristic
        for src in _extract_source_tokens(r.signal_reason or "" + " " + (r.judge_reason or "")):
            source_acc.setdefault(src, []).append(float(r.closed_pnl_pct))
        if r.verdict == "place" and float(r.closed_pnl_pct) < 0:
            losing.append({
                "symbol": r.symbol,
                "verdict": r.verdict,
                "pnl_pct": float(r.closed_pnl_pct),
                "judge_reason": (r.judge_reason or "")[:400],
                "intel_score": r.intel_score,
                "regime": r.regime,
            })

    with Session(engine) as session:
        unblock_rows = (
            session.query(UnblockDebateRun)
            .filter(UnblockDebateRun.run_at >= cutoff)
            .all()
        )
    report.n_unblock_debates = len(unblock_rows)
    for r in unblock_rows:
        if r.closed_pnl_pct is None:
            continue
        verdict_acc.setdefault(f"unblock_{r.verdict}", []).append(float(r.closed_pnl_pct))

    with Session(engine) as session:
        hold_rows = (
            session.query(HoldDebateRun)
            .filter(HoldDebateRun.run_at >= cutoff)
            .all()
        )
    report.n_hold_debates = len(hold_rows)
    for r in hold_rows:
        if r.resulting_pnl_pct is None:
            continue
        verdict_acc.setdefault(f"hold_{r.verdict}", []).append(float(r.resulting_pnl_pct))

    # Per-verdict aggregates
    place_pnls = verdict_acc.get("place", [])
    skip_pnls = verdict_acc.get("skip", [])
    if place_pnls:
        wins = sum(1 for p in place_pnls if p > 0)
        report.overall_place_winrate = round(wins / len(place_pnls), 3)
    if skip_pnls:
        # 'skip' winrate is interpreted as: skipped trades whose subsequent
        # price fell (i.e. our skip was correct). closed_pnl_pct on a skip
        # row should be the would-have-been P&L (reconciler responsibility).
        wins = sum(1 for p in skip_pnls if p < 0)
        report.overall_skip_winrate = round(wins / len(skip_pnls), 3)

    for verdict, pnls in verdict_acc.items():
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        report.per_verdict_winrate[verdict] = {
            "n": len(pnls),
            "winrate": round(wins / len(pnls), 3),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        }

    for src, pnls in source_acc.items():
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        report.per_source_winrate[src] = {
            "n": len(pnls),
            "winrate": round(wins / len(pnls), 3),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        }

    # Sort losing samples by P&L (worst first)
    losing.sort(key=lambda d: d["pnl_pct"])
    report.losing_patterns = losing[:sample_losing]
    report.shadow_skips = shadow_candidates[:sample_skips]
    report.n_trades_closed = sum(len(v) for v in verdict_acc.values())
    return report


def _extract_source_tokens(text: str) -> list[str]:
    """Heuristic: pick out source names that appear in signal/judge reasons.

    Uses the canonical source names registered in aggregator.SOURCE_WEIGHTS.
    Returns deduped lowercase matches.
    """
    if not text:
        return []
    from trading_bot.intel.aggregator import SOURCE_WEIGHTS
    text_lower = text.lower()
    out: set[str] = set()
    for src in SOURCE_WEIGHTS.keys():
        if src in text_lower:
            out.add(src)
    return sorted(out)


# ---------------------------------------------------------------------------
# Lesson persistence
# ---------------------------------------------------------------------------


def write_lesson(
    engine,
    *,
    report: OutcomeReport,
    summary_text: str,
    candidate_edits: list[dict] | None = None,
    prompt_version: str = "",
    now: dt.datetime | None = None,
) -> int:
    """Persist a DebateLesson row for the just-completed analysis run."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        session.add(DebateLesson(
            analysis_date=now,
            lookback_days=report.lookback_days,
            n_trades_closed=report.n_trades_closed,
            n_entry_debates=report.n_entry_debates,
            n_unblock_debates=report.n_unblock_debates,
            n_hold_debates=report.n_hold_debates,
            overall_place_winrate=report.overall_place_winrate,
            overall_skip_winrate=report.overall_skip_winrate,
            summary_text=summary_text or "",
            per_source_winrate_json=json.dumps(report.per_source_winrate, sort_keys=True),
            per_verdict_winrate_json=json.dumps(report.per_verdict_winrate, sort_keys=True),
            losing_patterns_json=json.dumps(report.losing_patterns),
            shadow_skips_json=json.dumps(report.shadow_skips),
            candidate_edits_json=json.dumps(candidate_edits or []),
            prompt_version=prompt_version,
        ))
        session.commit()
    return 1


def latest_lesson(engine) -> Optional[DebateLesson]:
    with Session(engine) as session:
        row = (
            session.query(DebateLesson)
            .order_by(_desc(DebateLesson.analysis_date))
            .first()
        )
        if row is not None:
            session.expunge(row)
    return row


# ---------------------------------------------------------------------------
# Brief injection
# ---------------------------------------------------------------------------


def latest_lesson_block(
    engine,
    *,
    max_age_days: int = DEFAULT_LESSON_FRESHNESS_DAYS,
    now: dt.datetime | None = None,
) -> str:
    """Format the latest DebateLesson into a brief-ready text block.

    Returns "" when:
      - no lesson exists
      - latest lesson is older than max_age_days (stale → don't inject
        misleading context)
      - lesson body is empty

    The string format is designed to drop directly under a "RECENT LESSONS"
    heading in any debate brief — short, scannable, with concrete numbers.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    row = latest_lesson(engine)
    if row is None:
        return ""
    analysis_date = row.analysis_date
    if analysis_date.tzinfo is None:
        analysis_date = analysis_date.replace(tzinfo=dt.timezone.utc)
    age_days = (now - analysis_date).total_seconds() / 86400.0
    if age_days > max_age_days:
        return ""
    summary = (row.summary_text or "").strip()
    if not summary:
        return ""
    # Compose with the topline numbers + the analyst's prose summary.
    parts: list[str] = []
    parts.append(
        f"(based on {row.n_trades_closed} closed trades, "
        f"last {row.lookback_days}d; analysis_date={analysis_date.date().isoformat()})"
    )
    if row.overall_place_winrate is not None:
        parts.append(
            f"Overall place-verdict winrate: {row.overall_place_winrate*100:.0f}%"
        )
    parts.append(summary)
    return "\n".join(parts)
