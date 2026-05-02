"""Compute health + last-activity for every node in the topology.

Reads two sources:
* ``role_runs`` — ok/warn/fail for any node with ``role_name``.
* ``events`` — last-activity timestamp for nodes that subscribe to bus
  event types (computed as max(created_at) over the subscribed set).

Returns a flat dict ``node_id -> {"health", "last_activity_ts",
"last_activity_label"}``. The dashboard converts the timestamp to a
relative age string ("2m ago"); ``system.js`` repaints health dots on
the periodic refresh.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.dashboard import system_topology as topo

# Health window thresholds (seconds since last activity).
# These are coarse — Phase 6 will refine with per-role cadence-aware
# rules. For Phase 5 a simple "last activity within 30/60/180 min"
# ladder is enough to give the operator a useful at-a-glance signal.
_OK_AGE_S = 30 * 60
_WARN_AGE_S = 60 * 60
_FAIL_AGE_S = 3 * 60 * 60


def _relative_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _classify(age_s: int | None) -> str:
    if age_s is None:
        return "off"
    if age_s <= _OK_AGE_S:
        return "ok"
    if age_s <= _WARN_AGE_S:
        return "warn"
    if age_s <= _FAIL_AGE_S:
        return "warn"  # still warn — fail only on actual error rows
    return "fail"


def _open_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)


def _last_role_run(conn: sqlite3.Connection, role_name: str) -> tuple[datetime | None, str | None]:
    """Return (started_at, status) for the most recent role run."""
    try:
        row = conn.execute(
            "SELECT started_at, status FROM role_runs "
            "WHERE role_name = ? ORDER BY started_at DESC LIMIT 1",
            (role_name,),
        ).fetchone()
    except Exception:
        return None, None
    if not row:
        return None, None
    started_str, status = row
    try:
        ts = datetime.fromisoformat(started_str) if isinstance(started_str, str) else started_str
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        ts = None
    return ts, status


def _last_event_ts(conn: sqlite3.Connection, types: tuple[str, ...]) -> datetime | None:
    if not types:
        return None
    placeholders = ",".join(["?"] * len(types))
    try:
        row = conn.execute(
            f"SELECT MAX(created_at) FROM events WHERE type IN ({placeholders})",
            types,
        ).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    val = row[0]
    try:
        ts = datetime.fromisoformat(val) if isinstance(val, str) else val
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def build_system_snapshot(db_path: str | Path = "data/state.db") -> dict[str, dict[str, Any]]:
    """Compute health + last-activity for every node. Cheap (a few
    indexed queries on small tables); fine to call on every page load.
    """
    out: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    try:
        conn = _open_ro(str(db_path))
    except Exception:
        # No DB yet (cold-start). Mark everything off.
        for n in topo.NODES:
            out[n.id] = {"health": "off", "last_activity_ts": None,
                         "last_activity_label": ""}
        return out

    try:
        for n in topo.NODES:
            if n.passive:
                # Passive intake nodes don't have health.
                out[n.id] = {"health": "off", "last_activity_ts": None,
                             "last_activity_label": ""}
                continue

            # Combine signals:
            # 1. The latest role_run drives the *baseline* health: error → fail,
            #    success → age-classified.
            # 2. If a subscribed event is *newer* than the latest role_run,
            #    its timestamp drives the age classification (a recent event
            #    means the box is doing its job *right now*, even if the role
            #    log hasn't been updated yet).
            role_ts: datetime | None = None
            role_status: str | None = None
            ev_ts: datetime | None = None
            if n.role_name:
                role_ts, role_status = _last_role_run(conn, n.role_name)
            if n.subscribes:
                ev_ts = _last_event_ts(conn, n.subscribes)

            ts = max((t for t in (role_ts, ev_ts) if t is not None), default=None)
            if ts is None:
                health = "off"
            elif role_status == "error" and (ev_ts is None or (role_ts and ev_ts <= role_ts)):
                # Most-recent signal is an errored role run.
                health = "fail"
            else:
                age_s = int((now - ts).total_seconds())
                health = _classify(age_s)

            label = _relative_age(int((now - ts).total_seconds())) if ts else ""
            out[n.id] = {
                "health": health,
                "last_activity_ts": ts.isoformat() if ts else None,
                "last_activity_label": label,
            }
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out
