"""Crypto lesson loop (Phase 1D).

Closes the feedback loop:
  hold debate verdicts + scout debate verdicts + closed-trade P&L
        ↓
  aggregator computes per-source / per-trigger / per-chain / per-funding-band winrates
        ↓
  Theo Marchetti (Opus) reads aggregates, drafts ``summary_text`` + candidate prompt edits
        ↓
  ``debate_lessons_crypto`` row written
        ↓
  next scout/hold debate brief reads it as the ``RECENT LESSONS`` block

Aggregation is done in-process (no LLM) so the lesson loop is cheap to
re-run; the LLM call is only for the human-readable summary + the
candidate-prompt-edits draft.

Outcome attribution (Phase 1D MVP scope):
  - hold debate verdicts pulled from ``hold_debate_runs_crypto``;
    ``resulting_pnl_pct`` is the win signal (None when not yet
    backfilled — those rows count toward N but not winrates).
  - scout debate verdicts from ``scout_debate_runs_crypto``;
    ``elevate`` verdicts that turned into winning trades go into
    per-source winrate via the candidate's ``sources_json``.
  - per-chain attribution joins on ``intel_candidates_crypto.symbol``
    to get the chain context.
  - per-funding-band attribution: derived from the most recent
    ``binance_funding`` event for the symbol at debate time. When no
    funding event is available, bucket = 'unknown'.

Once Phase 1G adds an entry-debate snapshot table, this module gains a
``per_persona_winrate_json`` dimension too — pre-wired in the JSON shape.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import (
    DebateLessonCrypto,
    HoldDebateRunCrypto,
    IntelCandidateCrypto,
    IntelEventCrypto,
    ScoutDebateRunCrypto,
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

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n, "wins": self.wins,
            "winrate_pct": (round(self.winrate_pct, 2)
                            if self.winrate_pct is not None else None),
            "avg_pnl_pct": (round(self.avg_pnl_pct, 3)
                            if self.avg_pnl_pct is not None else None),
        }


@dataclass
class OutcomeReport:
    lookback_days: int
    n_trades_closed: int = 0
    n_hold_debates: int = 0
    n_scout_debates: int = 0
    per_verdict: Dict[str, WinRate] = field(default_factory=lambda: defaultdict(WinRate))
    per_trigger: Dict[str, WinRate] = field(default_factory=lambda: defaultdict(WinRate))
    per_source:  Dict[str, WinRate] = field(default_factory=lambda: defaultdict(WinRate))
    per_chain:   Dict[str, WinRate] = field(default_factory=lambda: defaultdict(WinRate))
    per_funding_band: Dict[str, WinRate] = field(default_factory=lambda: defaultdict(WinRate))


# ---------------------------------------------------------------------------
# Funding-band classifier (used to bucket entry-time funding rate)
# ---------------------------------------------------------------------------


def _classify_funding_band(rate: Optional[float]) -> str:
    """Map funding rate to a band string. Bands match the plan's spec."""
    if rate is None:
        return "unknown"
    abs_r = abs(float(rate))
    if abs_r < 0.0003:
        return "low"
    if abs_r < 0.0010:
        return "neutral"
    if abs_r < 0.0015:
        return "high"
    return "extreme"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_outcomes(
    engine: Any,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: Optional[dt.datetime] = None,
) -> OutcomeReport:
    """Walk the last N days of crypto debate audit rows and roll up
    per-source/trigger/chain/funding-band winrates.

    Pure DB read — no LLM call. Idempotent.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=lookback_days)
    report = OutcomeReport(lookback_days=lookback_days)

    # Map symbol → (chain, sources_dict) for join-by-memo
    chain_by_symbol: Dict[str, Optional[str]] = {}
    sources_by_symbol: Dict[str, Dict[str, int]] = {}
    funding_band_by_symbol: Dict[str, str] = {}

    with Session(engine) as session:
        # Symbol lookup: use intel_events_crypto for chain (most-recent non-null)
        events = (
            session.query(IntelEventCrypto)
            .filter(IntelEventCrypto.ingested_at >= cutoff)
            .all()
        )
        for ev in events:
            if ev.chain and ev.symbol not in chain_by_symbol:
                chain_by_symbol[ev.symbol] = ev.chain
            if ev.source == "binance_funding" and ev.raw_score is not None:
                # Last seen funding rate per symbol → band
                funding_band_by_symbol.setdefault(
                    ev.symbol, _classify_funding_band(ev.raw_score),
                )

        # Sources lookup from intel_candidates_crypto
        for cand in session.query(IntelCandidateCrypto).all():
            try:
                sources_by_symbol[cand.symbol] = json.loads(cand.sources_json or "{}")
            except json.JSONDecodeError:
                sources_by_symbol[cand.symbol] = {}

        # ---- Hold debate outcomes -----------------------------------
        hold_runs = (
            session.query(HoldDebateRunCrypto)
            .filter(HoldDebateRunCrypto.run_at >= cutoff)
            .all()
        )
        report.n_hold_debates = len(hold_runs)
        for run in hold_runs:
            pnl = run.resulting_pnl_pct
            won = _hold_outcome_to_won(run.verdict, pnl)
            if won is not None:
                report.n_trades_closed += 1
            report.per_verdict[run.verdict].add(won=won, pnl_pct=pnl)
            if run.trigger_reason:
                report.per_trigger[run.trigger_reason].add(won=won, pnl_pct=pnl)
            chain = chain_by_symbol.get(run.symbol)
            if chain:
                report.per_chain[chain].add(won=won, pnl_pct=pnl)
            band = funding_band_by_symbol.get(run.symbol, "unknown")
            report.per_funding_band[band].add(won=won, pnl_pct=pnl)

        # ---- Scout debate outcomes -----------------------------------
        scout_runs = (
            session.query(ScoutDebateRunCrypto)
            .filter(ScoutDebateRunCrypto.run_at >= cutoff)
            .all()
        )
        report.n_scout_debates = len(scout_runs)
        for run in scout_runs:
            # Scout outcome is implicit: 'elevate' that resulted in any closed
            # winning hold = win; 'dismiss' that would have won (shadow) = miss.
            # MVP simplification: count elevate as 'success' if symbol later had
            # any positive hold-debate pnl_pct in the same lookback window.
            won = _scout_outcome_to_won(run.verdict, hold_runs, run.symbol)
            sources = sources_by_symbol.get(run.symbol, {})
            for source_name, count in sources.items():
                report.per_source[source_name].add(won=won, pnl_pct=None)

    return report


def _hold_outcome_to_won(verdict: str, pnl_pct: Optional[float]) -> Optional[bool]:
    """Translate a hold-debate verdict + realised pnl into a binary win/loss.

    Conventions:
      - hold:        won = pnl_pct >= 0  (we held; if it ended green, we won)
      - tighten_stop: won = pnl_pct >= 0
      - exit_now:    won = pnl_pct <= 0  (we cut to avoid loss; if it kept dropping
                     after we exited, the cut was correct — but we don't have that
                     data here. Use realised exit pnl as proxy: small loss is OK,
                     small gain on exit means we maybe over-cut)
      - None pnl:    return None (not yet backfilled)
    """
    if pnl_pct is None:
        return None
    if verdict == "exit_now":
        # Protective exits are 'wins' when pnl is small (we cut early before the cascade)
        return pnl_pct >= -2.0
    return pnl_pct >= 0.0


def _scout_outcome_to_won(
    verdict: str,
    hold_runs: Sequence[HoldDebateRunCrypto],
    symbol: str,
) -> Optional[bool]:
    """Scout outcome: did any hold debate on this symbol go positive?"""
    if verdict != "elevate":
        return None  # dismissals are tracked via shadow path (Phase 1D follow-on)
    related = [r for r in hold_runs
               if r.symbol == symbol and r.resulting_pnl_pct is not None]
    if not related:
        return None
    return any(r.resulting_pnl_pct >= 0 for r in related)


# ---------------------------------------------------------------------------
# Lesson rendering: turn an OutcomeReport into a brief block + lesson row
# ---------------------------------------------------------------------------


def _serialise_winrates(d: Dict[str, WinRate]) -> Dict[str, Dict[str, Any]]:
    return {k: v.as_dict() for k, v in sorted(d.items())}


def render_outcomes_block(report: OutcomeReport) -> str:
    """Format the OutcomeReport as the ``outcomes_block`` for Theo's prompt."""
    lines: List[str] = [
        f"Lookback window: {report.lookback_days} days",
        f"Hold debates: {report.n_hold_debates}, scout debates: {report.n_scout_debates}, "
        f"closed trades with pnl: {report.n_trades_closed}",
        "",
        "Per-verdict winrate:",
    ]
    for k, v in sorted(report.per_verdict.items()):
        lines.append(f"  {k:14}  n={v.n:3}  winrate={v.winrate_pct}  avg_pnl_pct={v.avg_pnl_pct}")
    lines.append("")
    lines.append("Per-trigger winrate (hold debates):")
    for k, v in sorted(report.per_trigger.items()):
        lines.append(f"  {k:18}  n={v.n:3}  winrate={v.winrate_pct}  avg_pnl_pct={v.avg_pnl_pct}")
    lines.append("")
    lines.append("Per-source winrate (scout-elevated symbols):")
    for k, v in sorted(report.per_source.items()):
        lines.append(f"  {k:22}  n={v.n:3}  winrate={v.winrate_pct}")
    lines.append("")
    lines.append("Per-chain winrate:")
    for k, v in sorted(report.per_chain.items()):
        lines.append(f"  {k:14}  n={v.n:3}  winrate={v.winrate_pct}  avg_pnl_pct={v.avg_pnl_pct}")
    lines.append("")
    lines.append("Per-funding-band winrate:")
    for k, v in sorted(report.per_funding_band.items()):
        lines.append(f"  {k:14}  n={v.n:3}  winrate={v.winrate_pct}  avg_pnl_pct={v.avg_pnl_pct}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lesson persistence + injection
# ---------------------------------------------------------------------------


def write_lesson(
    engine: Any,
    *,
    report: OutcomeReport,
    summary_text: str,
    candidate_prompt_edits: Sequence[str] = (),
    prompt_version: str = "",
    now: Optional[dt.datetime] = None,
) -> int:
    """Persist a ``DebateLessonCrypto`` row from an aggregated report. Returns row id."""
    now = now or dt.datetime.now(dt.timezone.utc)
    row = DebateLessonCrypto(
        analysis_date=now,
        lookback_days=report.lookback_days,
        n_trades_closed=report.n_trades_closed,
        n_hold_debates=report.n_hold_debates,
        n_scout_debates=report.n_scout_debates,
        summary_text=summary_text or "",
        per_source_winrate_json=json.dumps(_serialise_winrates(report.per_source), sort_keys=True),
        per_trigger_winrate_json=json.dumps(_serialise_winrates(report.per_trigger), sort_keys=True),
        per_chain_winrate_json=json.dumps(_serialise_winrates(report.per_chain), sort_keys=True),
        per_funding_band_winrate_json=json.dumps(_serialise_winrates(report.per_funding_band), sort_keys=True),
        candidate_prompt_edits_json=json.dumps(list(candidate_prompt_edits)),
        prompt_version=prompt_version or "",
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        return row.id


def latest_lesson_block(
    engine: Any,
    *,
    max_age_days: int = 7,
    now: Optional[dt.datetime] = None,
) -> str:
    """Return the most recent ``DebateLessonCrypto.summary_text`` (with key
    metrics inlined) for use as the ``RECENT LESSONS`` block in next debates.

    Returns a placeholder string if no fresh lesson row exists. This is the
    function `scout_debate.run_scout_debate(lessons_block=...)` and
    `hold_debate.run_hold_debate(lessons_block=...)` should call instead of
    the static placeholder.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max_age_days)
    with Session(engine) as session:
        row = (
            session.query(DebateLessonCrypto)
            .filter(DebateLessonCrypto.analysis_date >= cutoff)
            .order_by(DebateLessonCrypto.analysis_date.desc())
            .first()
        )
    if row is None:
        return "(no fresh lessons available — analyzer has not run in the last 7 days)"

    parts: List[str] = [
        f"RECENT LESSONS (last {row.lookback_days}d, "
        f"{row.n_trades_closed} closed trades, "
        f"{row.n_hold_debates} hold + {row.n_scout_debates} scout debates):",
        row.summary_text or "(no summary yet)",
    ]

    # Inline the highest-signal metrics for quick scanning.
    try:
        per_source = json.loads(row.per_source_winrate_json or "{}")
        if per_source:
            top_sources = sorted(
                per_source.items(),
                key=lambda kv: -(kv[1].get("winrate_pct") or 0),
            )[:3]
            parts.append("Top per-source winrates:")
            for src, m in top_sources:
                parts.append(f"  {src}: n={m['n']} winrate={m.get('winrate_pct')}%")
    except json.JSONDecodeError:
        pass
    try:
        per_chain = json.loads(row.per_chain_winrate_json or "{}")
        if per_chain:
            parts.append("Per-chain winrates:")
            for ch, m in sorted(per_chain.items()):
                parts.append(
                    f"  {ch}: n={m['n']} winrate={m.get('winrate_pct')}% "
                    f"avg_pnl={m.get('avg_pnl_pct')}%"
                )
    except json.JSONDecodeError:
        pass
    try:
        per_band = json.loads(row.per_funding_band_winrate_json or "{}")
        if per_band:
            parts.append("Per-funding-band winrates:")
            for band, m in sorted(per_band.items()):
                parts.append(
                    f"  {band}: n={m['n']} winrate={m.get('winrate_pct')}%"
                )
    except json.JSONDecodeError:
        pass

    return "\n".join(parts)
