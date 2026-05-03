"""Per-stage runtime state for the Pipelines system page.

Walks every Stage in the topology, runs its ``count_query`` against
the live state DB, joins with role_runs to get last-status / last-run,
and returns a flat dict the template iterates.

Health logic (per stage):
  - "ok"   — role had a successful run today
  - "warn" — last run >24h ago OR last status was warn
  - "fail" — last status errored AND no successful run since
  - "off"  — role has never run in the audit window

Counts fail soft. A missing table or syntax error returns 0 with the
error logged so the page still renders. Single-DB queries are cheap;
no caching layer needed (the dashboard's per-second cache fronts this).
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading_bot.dashboard.pipelines_topology import (
    PIPELINES,
    Pipeline,
    Stage,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


@dataclass
class StageState:
    """Render-ready state for one stage card."""
    stage: Stage
    health: str = "off"                    # ok | warn | fail | off
    count: int = 0
    runs_today: int = 0
    last_run_at: Optional[dt.datetime] = None
    last_status: str = ""
    last_error: str = ""
    operators: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class PipelineState:
    pipeline: Pipeline
    stages: List[StageState]
    summary: str = ""                      # "✓ flowing" | "⚠ partial" | "✗ stalled"
    summary_tone: str = "off"              # ok | warn | fail | off


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _safe_count(conn: sqlite3.Connection, sql: Optional[str]) -> int:
    if not sql:
        return 0
    try:
        cur = conn.execute(sql.strip())
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        logger.debug("pipelines_state count query failed: %s — %s", sql.strip()[:80], e)
        return 0


def _role_health_row(
    conn: sqlite3.Connection,
    role_name: Optional[str],
    *,
    today_start_utc: dt.datetime,
) -> Dict[str, Any]:
    """Return last-run + counts for a role_name; fail-soft to empty dict."""
    if not role_name:
        return {}
    try:
        cur = conn.execute(
            """
            SELECT started_at, status, error_text
            FROM role_runs
            WHERE role_name = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (role_name,),
        )
        last = cur.fetchone()
        # Use SQLite's own datetime comparison (start-of-day UTC) so
        # SQLAlchemy's TEXT-with-microseconds storage compares correctly
        # against our isoformat strings.
        today_iso = today_start_utc.strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            """
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS today
            FROM role_runs
            WHERE role_name = ?
              AND started_at >= datetime('now', '-30 day')
            """,
            (today_iso, role_name),
        )
        counts = cur.fetchone()
        return {
            "last_run_at": _parse_dt(last[0]) if last else None,
            "last_status": (last[1] if last else "") or "",
            "last_error": (last[2] if last else "") or "",
            "runs_30d": int(counts[0] or 0) if counts else 0,
            "runs_today": int(counts[1] or 0) if counts else 0,
        }
    except Exception as e:
        logger.debug("role_health lookup failed for %s: %s", role_name, e)
        return {}


def _parse_dt(s: Any) -> Optional[dt.datetime]:
    if s is None:
        return None
    if isinstance(s, dt.datetime):
        return s if s.tzinfo else s.replace(tzinfo=dt.timezone.utc)
    try:
        # SQLite TEXT format: "YYYY-MM-DD HH:MM:SS[.fff]"
        text = str(s).replace("T", " ")
        parsed = dt.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Health logic
# ---------------------------------------------------------------------------


_LLM_OUTPUT_KINDS = {"scout", "entry", "hold", "wheel", "lesson"}


def _stage_health(
    stage: Stage,
    *,
    role_info: Dict[str, Any],
    count: int,
    now: dt.datetime,
) -> str:
    """Decide ok / warn / fail / off for a stage card.

    Rules:
      - Stages whose only job is a SQL count (state machines, aggregators
        that have no role_runs) report ok when count > 0, off otherwise.
      - LLM stages (scout/entry/hold/wheel/lesson) downgrade to warn when
        the host role ran but produced ZERO debates today — surfaces the
        common failure mode where the cron fires but no debates land.
      - Stages with a role_name look at role_runs:
          last status fail AND no recent success → fail
          last run >24h ago → warn
          last run today, ok → ok
          no runs ever → off
    """
    last = role_info.get("last_run_at")
    status = role_info.get("last_status") or ""
    runs_today = int(role_info.get("runs_today") or 0)

    if not stage.role_name:
        return "ok" if count > 0 else "off"

    if last is None:
        return "off"
    age_hours = (now - last).total_seconds() / 3600.0

    if status == "error":
        # Recent error → fail; older error superseded by today's success → ok
        return "fail" if runs_today == 0 else "warn"
    if age_hours > 36:
        return "warn"
    if runs_today == 0:
        return "warn"
    # LLM stages must produce output to be healthy. A scout-debate stage
    # whose role fired 10x today but wrote zero scout_debate_runs rows
    # is a broken pipeline, not a healthy idle one.
    if stage.kind in _LLM_OUTPUT_KINDS and count == 0:
        return "warn"
    return "ok"


def _pipeline_summary(stages: List[StageState]) -> tuple[str, str]:
    """Roll a column up to a one-liner: green/amber/red."""
    healths = [s.health for s in stages]
    if all(h == "off" for h in healths):
        return ("not firing — no stage has run", "off")
    if any(h == "fail" for h in healths):
        return ("partial — at least one stage is failing", "fail")
    if any(h == "warn" or h == "off" for h in healths):
        # Some stages stale or not running — still useful but partial
        running = sum(1 for h in healths if h == "ok")
        total = len(stages)
        return (f"partial — {running} / {total} stages flowing", "warn")
    return ("flowing — every stage healthy", "ok")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_pipelines_state(
    state_db: str | Path,
    *,
    now: Optional[dt.datetime] = None,
) -> List[PipelineState]:
    """Walk the topology, query the DB, return render-ready state."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today_start_utc = dt.datetime.combine(
        now.astimezone(dt.timezone.utc).date(),
        dt.time.min,
        tzinfo=dt.timezone.utc,
    )

    # Persona resolver — late import to avoid circulars.
    try:
        from trading_bot.shared.role_persona_map import _registry as _persona_registry
        persona_map = _persona_registry()
    except Exception:
        persona_map = {}

    out: List[PipelineState] = []
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except Exception as e:
        logger.warning("pipelines_state: cannot open DB %s: %s", state_db, e)
        # Fall back to off-state for every stage
        for pipe in PIPELINES:
            stages = [StageState(stage=s) for s in pipe.stages]
            out.append(PipelineState(
                pipeline=pipe, stages=stages,
                summary="state DB unavailable", summary_tone="fail",
            ))
        return out

    try:
        for pipe in PIPELINES:
            stage_states: List[StageState] = []
            for stage in pipe.stages:
                count = _safe_count(conn, stage.count_query)
                role_info = _role_health_row(
                    conn, stage.role_name, today_start_utc=today_start_utc,
                )
                health = _stage_health(stage, role_info=role_info, count=count, now=now)
                operators = []
                for pid in stage.persona_ids:
                    info = persona_map.get(pid)
                    if info is None:
                        continue
                    operators.append({
                        "persona_id": pid,
                        "name": info.full_name,
                        "title": info.role_title,
                        "debate_role": info.debate_role,
                        "is_judge": info.debate_role.endswith("_judge"),
                    })
                stage_states.append(StageState(
                    stage=stage,
                    health=health,
                    count=count,
                    runs_today=int(role_info.get("runs_today") or 0),
                    last_run_at=role_info.get("last_run_at"),
                    last_status=str(role_info.get("last_status") or ""),
                    last_error=str(role_info.get("last_error") or "")[:160],
                    operators=operators,
                ))
            summary, tone = _pipeline_summary(stage_states)
            out.append(PipelineState(
                pipeline=pipe, stages=stage_states,
                summary=summary, summary_tone=tone,
            ))
    finally:
        conn.close()
    return out
