"""Per-persona accuracy stats for the /desk roster.

The aggregator queries every debate audit table across pipelines and
computes per-persona metrics:

  Judge personas (model_tier=judge):
    - n_verdicts: total verdicts cast
    - n_outcomes_known: verdicts whose downstream outcome is recorded
    - hit_rate_pct: fraction of place/elevate verdicts that turned
      profitable + fraction of skip/dismiss verdicts that avoided losses
    - last_run_at: most recent verdict timestamp

  Reviewer personas (model_tier=reviewer):
    - n_runs: number of debates this reviewer participated in
    - last_run_at: most recent participation
    (Reviewers don't produce verdicts, so hit-rate doesn't apply.)

  Lesson / classifier / summary personas:
    - n_runs: number of times invoked
    - last_run_at: most recent invocation

This module is a read-only join helper called by the dashboard at
``/desk`` render time. No writes; no caching layer (the dashboard's
own per-second cache absorbs traffic).

Per-pipeline tables read:
  Crypto:
    scout_debate_runs_crypto  (judge=Diane Pereira / scout_judge variant)
    hold_debate_runs_crypto   (judge=Diane Pereira / hold_judge variant)
    entry_debate_runs_crypto  (judge=Diane Pereira / entry_judge variant)
  Options:
    scout_debate_runs_options (judge=Marcus Whitfield)
    wheel_debate_runs_options (judge=Catherine Lloyd)
  Stocks:
    scout_debate_runs         (legacy, judge=Margaret Holloway-style)
    hold_debate_runs          (legacy)
    entry_debate_runs         (legacy)

Per-persona attribution comes from the prompt_version field:
  ``crypto_scout/skeptic=v1,analyst=v1,judge=v1`` →
    skeptic, analyst, judge each contributed one run.

The hit-rate logic is intentionally lenient on missing outcome data —
unknown outcomes are excluded from the denominator rather than counted
as misses.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


@dataclass
class PersonaStats:
    """Per-persona stats payload rendered on a /desk card."""
    debate_role: str
    pipeline: str
    n_runs: int = 0
    n_verdicts: int = 0
    n_outcomes_known: int = 0
    n_correct: int = 0
    last_run_at: Optional[dt.datetime] = None

    @property
    def hit_rate_pct(self) -> Optional[float]:
        if self.n_outcomes_known <= 0:
            return None
        return round(100.0 * self.n_correct / self.n_outcomes_known, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_runs": self.n_runs,
            "n_verdicts": self.n_verdicts,
            "n_outcomes_known": self.n_outcomes_known,
            "hit_rate_pct": self.hit_rate_pct,
            "last_run_at": (
                self.last_run_at.isoformat() if self.last_run_at else None
            ),
        }


# ---------------------------------------------------------------------------
# Judging convention — what counts as "correct"
# ---------------------------------------------------------------------------
# A judge's verdict is "correct" when:
#   - place verdict → trade closed with positive P&L
#   - skip / dismiss verdict → no follow-up trade was opened (best-effort proxy:
#     no entry within 24h of the verdict on the same symbol). Without that
#     join we count a skip as known-correct only when no later place verdict
#     for the same symbol occurred within the lookback window.


def _verdict_is_positive(verdict_str: str) -> bool:
    return verdict_str.lower() in ("place", "elevate")


def _verdict_is_negative(verdict_str: str) -> bool:
    return verdict_str.lower() in ("skip", "dismiss")


# ---------------------------------------------------------------------------
# Audit-table scanners
# ---------------------------------------------------------------------------


def _has_table(engine: Any, table_name: str) -> bool:
    try:
        return sa_inspect(engine).has_table(table_name)
    except Exception:
        return False


def _split_prompt_version(version: str) -> Dict[str, str]:
    """Parse a prompt_version string like::

        crypto_scout/skeptic=v1,analyst=v1,judge=v1

    into ``{"skeptic": "v1", "analyst": "v1", "judge": "v1"}``. Returns
    an empty dict when the format isn't recognised.
    """
    if not version:
        return {}
    if "/" in version:
        _, _, rest = version.partition("/")
    else:
        rest = version
    out: Dict[str, str] = {}
    for chunk in rest.split(","):
        if "=" in chunk:
            k, _, v = chunk.partition("=")
            out[k.strip()] = v.strip()
    return out


def _accumulate_role(
    accum: Dict[tuple[str, str], PersonaStats],
    *,
    debate_role: str,
    pipeline: str,
    run_at: Optional[dt.datetime],
    is_judge: bool = False,
    verdict_correct: Optional[bool] = None,
) -> None:
    key = (pipeline, debate_role)
    stats = accum.setdefault(
        key,
        PersonaStats(debate_role=debate_role, pipeline=pipeline),
    )
    stats.n_runs += 1
    if run_at is not None and (
        stats.last_run_at is None or run_at > stats.last_run_at
    ):
        stats.last_run_at = run_at
    if is_judge:
        stats.n_verdicts += 1
        if verdict_correct is True:
            stats.n_outcomes_known += 1
            stats.n_correct += 1
        elif verdict_correct is False:
            stats.n_outcomes_known += 1


# ---------------------------------------------------------------------------
# Pipeline-specific scanners
# ---------------------------------------------------------------------------


def _scan_crypto(
    engine: Any,
    *,
    accum: Dict[tuple[str, str], PersonaStats],
    cutoff: dt.datetime,
) -> None:
    """Scan crypto debate audit tables and update accum in place."""
    if not _has_table(engine, "scout_debate_runs_crypto"):
        return
    from trading_bot.pipelines.crypto.state_db import (
        EntryDebateRunCrypto,
        HoldDebateRunCrypto,
        ScoutDebateRunCrypto,
    )

    with Session(engine) as session:
        # Scout debate (skeptic + analyst + judge per row)
        scout_rows = (
            session.query(ScoutDebateRunCrypto)
            .filter(ScoutDebateRunCrypto.run_at >= cutoff)
            .all()
        )
        for row in scout_rows:
            roles = _split_prompt_version(row.prompt_version)
            for role_key in ("skeptic", "analyst"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"scout_{role_key}",
                        pipeline="crypto", run_at=row.run_at,
                    )
            # Judge — outcome unknown (scout debates don't have a P&L outcome
            # at the row level; verdict tracking only)
            _accumulate_role(
                accum, debate_role="scout_judge", pipeline="crypto",
                run_at=row.run_at, is_judge=True,
                # crypto scout: elevate verdicts that did NOT later get held
                # are unknown; we leave correctness as None until lesson_loop
                # joins in.
                verdict_correct=None,
            )

        # Hold debate (aggressive + conservative + neutral + judge per row)
        hold_rows = (
            session.query(HoldDebateRunCrypto)
            .filter(HoldDebateRunCrypto.run_at >= cutoff)
            .all()
        )
        for row in hold_rows:
            roles = _split_prompt_version(row.prompt_version)
            for role_key in ("aggressive", "conservative", "neutral"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"hold_{role_key}",
                        pipeline="crypto", run_at=row.run_at,
                    )
            # Judge correctness — exit_now should result in a stop or
            # tighten that protected P&L (resulting_pnl_pct >= entry_pnl).
            verdict_correct = None
            if row.resulting_pnl_pct is not None:
                if row.verdict == "exit_now":
                    verdict_correct = row.resulting_pnl_pct > -5.0  # avoided big loss
                elif row.verdict == "hold":
                    verdict_correct = row.resulting_pnl_pct > 0
                elif row.verdict == "tighten_stop":
                    verdict_correct = row.resulting_pnl_pct >= 0
            _accumulate_role(
                accum, debate_role="hold_judge", pipeline="crypto",
                run_at=row.run_at, is_judge=True,
                verdict_correct=verdict_correct,
            )

        # Entry debate
        entry_rows = (
            session.query(EntryDebateRunCrypto)
            .filter(EntryDebateRunCrypto.run_at >= cutoff)
            .all()
        )
        for row in entry_rows:
            roles = _split_prompt_version(row.prompt_version)
            for role_key in ("aggressive", "conservative", "neutral"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"entry_{role_key}",
                        pipeline="crypto", run_at=row.run_at,
                    )
            _accumulate_role(
                accum, debate_role="entry_judge", pipeline="crypto",
                run_at=row.run_at, is_judge=True,
                verdict_correct=None,  # P&L join lands later
            )


def _scan_options(
    engine: Any,
    *,
    accum: Dict[tuple[str, str], PersonaStats],
    cutoff: dt.datetime,
) -> None:
    if not _has_table(engine, "scout_debate_runs_options"):
        return
    from trading_bot.pipelines.options.state_db import (
        ScoutDebateRunOptions,
        WheelCycleOptions,
        WheelDebateRunOptions,
    )

    with Session(engine) as session:
        scout_rows = (
            session.query(ScoutDebateRunOptions)
            .filter(ScoutDebateRunOptions.run_at >= cutoff)
            .all()
        )
        for row in scout_rows:
            roles = _split_prompt_version(row.prompt_version)
            for role_key in ("skeptic", "analyst"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"scout_{role_key}",
                        pipeline="options", run_at=row.run_at,
                    )
            _accumulate_role(
                accum, debate_role="scout_judge", pipeline="options",
                run_at=row.run_at, is_judge=True,
                verdict_correct=None,
            )

        wheel_rows = (
            session.query(WheelDebateRunOptions)
            .filter(WheelDebateRunOptions.run_at >= cutoff)
            .all()
        )
        for row in wheel_rows:
            roles = _split_prompt_version(row.prompt_version)
            for role_key in ("aggressive", "conservative", "neutral"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"wheel_{role_key}",
                        pipeline="options", run_at=row.run_at,
                    )
            verdict_correct = None
            if row.cycle_id is not None and row.verdict == "place":
                cycle = session.get(WheelCycleOptions, row.cycle_id)
                if cycle is not None and cycle.realized_pnl is not None:
                    verdict_correct = cycle.realized_pnl > 0
            _accumulate_role(
                accum, debate_role="wheel_judge", pipeline="options",
                run_at=row.run_at, is_judge=True,
                verdict_correct=verdict_correct,
            )


def _scan_stocks(
    engine: Any,
    *,
    accum: Dict[tuple[str, str], PersonaStats],
    cutoff: dt.datetime,
) -> None:
    """Scan legacy stocks debate tables.

    Uses dynamic table introspection so a schema that doesn't have these
    tables yet (cold-start environment, fresh DB) returns silently.
    """
    if not _has_table(engine, "scout_debate_runs"):
        return
    try:
        from trading_bot.state_db import (  # type: ignore[attr-defined]
            ScoutDebateRun,
            HoldDebateRun,
            EntryDebateRun,
        )
    except ImportError:
        # Tables missing or model not yet relocated → silent skip.
        return

    with Session(engine) as session:
        try:
            scout_rows = (
                session.query(ScoutDebateRun)
                .filter(ScoutDebateRun.run_at >= cutoff)
                .all()
            )
        except Exception:
            scout_rows = []
        for row in scout_rows:
            roles = _split_prompt_version(getattr(row, "prompt_version", "") or "")
            for role_key in ("skeptic", "analyst"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"scout_{role_key}",
                        pipeline="stocks", run_at=row.run_at,
                    )
            _accumulate_role(
                accum, debate_role="scout_judge", pipeline="stocks",
                run_at=row.run_at, is_judge=True,
                verdict_correct=None,
            )

        try:
            hold_rows = (
                session.query(HoldDebateRun)
                .filter(HoldDebateRun.run_at >= cutoff)
                .all()
            )
        except Exception:
            hold_rows = []
        for row in hold_rows:
            roles = _split_prompt_version(getattr(row, "prompt_version", "") or "")
            for role_key in ("aggressive", "conservative", "neutral"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"hold_{role_key}",
                        pipeline="stocks", run_at=row.run_at,
                    )
            verdict_correct = None
            resulting_pnl = getattr(row, "resulting_pnl_pct", None)
            if resulting_pnl is not None:
                if row.verdict == "exit_now":
                    verdict_correct = resulting_pnl > -5.0
                elif row.verdict == "hold":
                    verdict_correct = resulting_pnl > 0
                elif row.verdict == "tighten_stop":
                    verdict_correct = resulting_pnl >= 0
            _accumulate_role(
                accum, debate_role="hold_judge", pipeline="stocks",
                run_at=row.run_at, is_judge=True,
                verdict_correct=verdict_correct,
            )

        try:
            entry_rows = (
                session.query(EntryDebateRun)
                .filter(EntryDebateRun.run_at >= cutoff)
                .all()
            )
        except Exception:
            entry_rows = []
        for row in entry_rows:
            roles = _split_prompt_version(getattr(row, "prompt_version", "") or "")
            for role_key in ("aggressive", "conservative", "neutral"):
                if role_key in roles:
                    _accumulate_role(
                        accum, debate_role=f"entry_{role_key}",
                        pipeline="stocks", run_at=row.run_at,
                    )
            _accumulate_role(
                accum, debate_role="entry_judge", pipeline="stocks",
                run_at=row.run_at, is_judge=True,
                verdict_correct=None,
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_persona_stats(
    engine: Any,
    *,
    lookback_days: int = 30,
    now: Optional[dt.datetime] = None,
) -> Dict[tuple[str, str], PersonaStats]:
    """Compute per-persona stats keyed by (pipeline, debate_role).

    Returns an empty dict when the audit tables are empty or absent; the
    dashboard renders the persona card without a hit-rate badge in that
    case (no debate has fired yet).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=lookback_days)

    accum: Dict[tuple[str, str], PersonaStats] = {}

    # Scan each pipeline; per-pipeline scanners are isolated so a missing
    # table in one pipeline never blocks another.
    try:
        _scan_crypto(engine, accum=accum, cutoff=cutoff)
    except Exception as e:  # noqa: BLE001
        logger.warning("persona_accuracy crypto scan failed: %s", e)
    try:
        _scan_options(engine, accum=accum, cutoff=cutoff)
    except Exception as e:  # noqa: BLE001
        logger.warning("persona_accuracy options scan failed: %s", e)
    try:
        _scan_stocks(engine, accum=accum, cutoff=cutoff)
    except Exception as e:  # noqa: BLE001
        logger.warning("persona_accuracy stocks scan failed: %s", e)

    # Re-key by (pipeline, debate_role) so dashboard lookup is direct.
    out: Dict[tuple[str, str], PersonaStats] = {}
    for stats in accum.values():
        out[(stats.pipeline, stats.debate_role)] = stats
    return out
