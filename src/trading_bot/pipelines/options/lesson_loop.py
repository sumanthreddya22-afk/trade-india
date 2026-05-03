"""Options lesson loop (Phase 3).

Closes the feedback loop on options debates:
  wheel debate verdicts + scout debate verdicts + closed-cycle P&L
        ↓
  aggregator computes per-source / per-IV-rank-band / per-DTE-band /
  per-structure winrates
        ↓
  Mira Bhatt (Opus) reads aggregates, drafts ``summary_text`` + candidate
  prompt edits
        ↓
  ``debate_lessons_options`` row written
        ↓
  next scout/wheel debate brief reads it as the ``RECENT LESSONS`` block

Mirrors the crypto lesson loop pattern (``pipelines.crypto.lesson_loop``)
but with options-native attribution dimensions:
  - per_iv_rank_band  (low <30 / mid 30-70 / high >70)
  - per_dte_band       (weekly <=14d / monthly 15-45d / quarterly >45d)
  - per_structure      (csp / cc / vertical)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from trading_bot.pipelines.options.state_db import (
    DebateLessonOptions,
    ScoutDebateRunOptions,
    WheelCycleOptions,
    WheelDebateRunOptions,
)

logger = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 14


# ---------------------------------------------------------------------------
# Aggregation result (pure data — no LLM yet)
# ---------------------------------------------------------------------------


@dataclass
class WinRate:
    n: int = 0
    wins: int = 0
    pnl_sum: float = 0.0

    def add(self, *, won: Optional[bool], pnl_pct: Optional[float]) -> None:
        self.n += 1
        if won is True:
            self.wins += 1
        if pnl_pct is not None:
            self.pnl_sum += float(pnl_pct)

    @property
    def winrate_pct(self) -> Optional[float]:
        return (self.wins / self.n * 100) if self.n > 0 else None

    @property
    def avg_pnl_pct(self) -> Optional[float]:
        return (self.pnl_sum / self.n) if self.n > 0 else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "winrate_pct": (
                round(self.winrate_pct, 2) if self.winrate_pct is not None else None
            ),
            "avg_pnl_pct": (
                round(self.avg_pnl_pct, 4) if self.avg_pnl_pct is not None else None
            ),
        }


@dataclass
class OptionsLessonAggregates:
    """Pure aggregation result for one analysis window."""
    analysis_date: dt.datetime
    lookback_days: int
    n_cycles_closed: int = 0
    n_wheel_debates: int = 0
    n_scout_debates: int = 0
    per_source: Dict[str, WinRate] = field(default_factory=dict)
    per_iv_rank_band: Dict[str, WinRate] = field(default_factory=dict)
    per_dte_band: Dict[str, WinRate] = field(default_factory=dict)
    per_structure: Dict[str, WinRate] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Bucketing helpers
# ---------------------------------------------------------------------------


def _iv_rank_band(iv_rank: Optional[float]) -> str:
    if iv_rank is None:
        return "unknown"
    if iv_rank < 30:
        return "low"
    if iv_rank < 70:
        return "mid"
    return "high"


def _dte_band(dte: Optional[int]) -> str:
    if dte is None:
        return "unknown"
    if dte <= 14:
        return "weekly"
    if dte <= 45:
        return "monthly"
    return "quarterly"


# ---------------------------------------------------------------------------
# Aggregation entry point
# ---------------------------------------------------------------------------


def aggregate_outcomes(
    engine: Any,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: Optional[dt.datetime] = None,
) -> OptionsLessonAggregates:
    """Walk the options debate audit tables + wheel cycles, bucket
    outcomes by attribution dimensions, return aggregates."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=lookback_days)

    out = OptionsLessonAggregates(
        analysis_date=now, lookback_days=lookback_days,
    )
    per_source: Dict[str, WinRate] = defaultdict(WinRate)
    per_iv: Dict[str, WinRate] = defaultdict(WinRate)
    per_dte: Dict[str, WinRate] = defaultdict(WinRate)
    per_struct: Dict[str, WinRate] = defaultdict(WinRate)

    with Session(engine) as session:
        # Closed cycles: realized_pnl is the win signal.
        cycles = (
            session.query(WheelCycleOptions)
            .filter(WheelCycleOptions.ended_at.isnot(None))
            .filter(WheelCycleOptions.ended_at >= cutoff)
            .all()
        )
        out.n_cycles_closed = len(cycles)

        for cyc in cycles:
            pnl = cyc.realized_pnl
            won = pnl is not None and pnl > 0
            # Find the matching wheel-debate row to learn IV rank, DTE, structure
            wheel_run = (
                session.query(WheelDebateRunOptions)
                .filter(WheelDebateRunOptions.underlying == cyc.underlying)
                .filter(WheelDebateRunOptions.cycle_id == cyc.id)
                .order_by(WheelDebateRunOptions.run_at.asc())
                .first()
            )
            if wheel_run is not None:
                per_iv[_iv_rank_band(wheel_run.iv_rank)].add(won=won, pnl_pct=pnl)
                per_dte[_dte_band(wheel_run.chosen_dte_days or wheel_run.proposed_dte_days)].add(
                    won=won, pnl_pct=pnl,
                )
                struct = wheel_run.chosen_structure or "csp"
                per_struct[struct].add(won=won, pnl_pct=pnl)

        # Scout debate counts (no per-symbol P&L attribution at scout level —
        # that comes from elevated→entered→closed cycles)
        out.n_scout_debates = (
            session.query(ScoutDebateRunOptions)
            .filter(ScoutDebateRunOptions.run_at >= cutoff)
            .count()
        )
        # Wheel debate counts
        out.n_wheel_debates = (
            session.query(WheelDebateRunOptions)
            .filter(WheelDebateRunOptions.run_at >= cutoff)
            .count()
        )

    # Per-source attribution: best-effort. The scout debate's
    # ScoutDebateRunOptions.top_reason carries the dominant source,
    # but mapping reason → source needs a parser. For now, source
    # attribution is the JSON ``sources_json`` from the candidate row
    # at debate time. Phase 3+ will plumb a per-debate snapshot.

    out.per_source = dict(per_source)
    out.per_iv_rank_band = dict(per_iv)
    out.per_dte_band = dict(per_dte)
    out.per_structure = dict(per_struct)
    return out


# ---------------------------------------------------------------------------
# Lesson row write
# ---------------------------------------------------------------------------


def write_lesson_row(
    engine: Any,
    *,
    aggregates: OptionsLessonAggregates,
    summary_text: str,
    candidate_prompt_edits: List[str],
    prompt_version: str,
) -> int:
    """Persist a DebateLessonOptions row. Returns the new row id."""
    def _serialize(buckets: Dict[str, WinRate]) -> str:
        return json.dumps(
            {k: v.to_dict() for k, v in buckets.items()},
            sort_keys=True, default=str,
        )

    with Session(engine) as session:
        row = DebateLessonOptions(
            analysis_date=aggregates.analysis_date,
            lookback_days=aggregates.lookback_days,
            n_cycles_closed=aggregates.n_cycles_closed,
            n_wheel_debates=aggregates.n_wheel_debates,
            n_scout_debates=aggregates.n_scout_debates,
            summary_text=summary_text or "",
            per_source_winrate_json=_serialize(aggregates.per_source),
            per_iv_rank_band_winrate_json=_serialize(aggregates.per_iv_rank_band),
            per_dte_band_winrate_json=_serialize(aggregates.per_dte_band),
            per_structure_winrate_json=_serialize(aggregates.per_structure),
            candidate_prompt_edits_json=json.dumps(candidate_prompt_edits),
            prompt_version=prompt_version,
        )
        session.add(row)
        session.commit()
        return row.id


# ---------------------------------------------------------------------------
# Lesson block reader (used by next debate brief)
# ---------------------------------------------------------------------------


def latest_lesson_block(
    engine: Any,
    *,
    max_age_days: int = 7,
    now: Optional[dt.datetime] = None,
) -> str:
    """Return the most recent ``DebateLessonOptions.summary_text`` plus key
    metrics inlined, for use as the ``RECENT LESSONS`` block."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max_age_days)
    with Session(engine) as session:
        row = (
            session.query(DebateLessonOptions)
            .filter(DebateLessonOptions.analysis_date >= cutoff)
            .order_by(DebateLessonOptions.analysis_date.desc())
            .first()
        )
    if row is None:
        return (
            "(no fresh options lessons available — analyzer has not run "
            "in the last 7 days)"
        )

    parts: List[str] = [
        f"RECENT OPTIONS LESSONS (last {row.lookback_days}d, "
        f"{row.n_cycles_closed} cycles closed, "
        f"{row.n_wheel_debates} wheel + {row.n_scout_debates} scout debates):",
        row.summary_text or "(no summary yet)",
    ]
    try:
        per_iv = json.loads(row.per_iv_rank_band_winrate_json or "{}")
        if per_iv:
            parts.append("Per-IV-rank-band winrates:")
            for band in ("low", "mid", "high", "unknown"):
                m = per_iv.get(band)
                if not m:
                    continue
                parts.append(
                    f"  {band}: n={m['n']} winrate={m.get('winrate_pct')}% "
                    f"avg_pnl={m.get('avg_pnl_pct')}%"
                )
    except json.JSONDecodeError:
        pass
    try:
        per_struct = json.loads(row.per_structure_winrate_json or "{}")
        if per_struct:
            parts.append("Per-structure winrates:")
            for struct, m in sorted(per_struct.items()):
                parts.append(
                    f"  {struct}: n={m['n']} winrate={m.get('winrate_pct')}%"
                )
    except json.JSONDecodeError:
        pass
    return "\n".join(parts)
