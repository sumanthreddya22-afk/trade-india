"""Compute health + last-activity for every node in the topology.

Reads three sources:
* ``role_runs`` — ok/warn/fail for any node with ``role_name``.
* ``events`` — last-activity timestamp for nodes that subscribe to bus
  event types (computed as max(created_at) over the subscribed set).
* ``data/scheduler_last_run.json`` — fallback for cron-driven jobs that
  don't run as BaseRoles (heartbeat, alert_drain, log_rotation, …).

Returns a flat dict ``node_id -> {"health", "last_activity_ts",
"last_activity_label"}``. The dashboard converts the timestamp to a
relative age string ("2m ago"); ``system.js`` repaints health dots on
the periodic refresh.

Health thresholds are cadence-aware: a daily job is "ok" within 26h of
its last run, a 60-min job within 75 min, etc. The mapping is derived
from the same cron table the Scheduled-Jobs card uses, so they stay in
lockstep.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.dashboard import system_topology as topo

# Fallback thresholds for nodes whose cadence is unknown.
_DEFAULT_OK_AGE_S = 30 * 60
_DEFAULT_WARN_AGE_S = 60 * 60
_DEFAULT_FAIL_AGE_S = 3 * 60 * 60


# Some topology nodes use a label that differs from the matching key in
# scheduler_last_run.json / _KNOWN_SCHEDULED_JOBS. Map them here so the
# fallback lookup finds the right row.
_TASK_ID_ALIAS: dict[str, str] = {
    # topology role_name -> scheduler_history task_id
    "verify_stops": "order_steward_sweep",
    "portfolio_watch": "portfolio_monitor",
    "lab_evolution": "param_search",      # nightly param search is the canonical "lab evolution" tick
    "reconciler": "reconciler_close",
    "wheel_scan": "wheel_scan",            # passthrough; included for clarity
    "wheel_universe_build": "wheel_universe_build",
    # Daily/weekly roles whose cadence is recorded under a different
    # scheduler task_id. Adding them here lets the cadence-aware
    # classifier know "yesterday's run is fine" for daily jobs.
    "universe_curator": "massive_refresh",
    "sentiment_analyst": "news_warm_morning",
    "vip_listener": "vip_listener",
    "iv_capture": "iv_capture",
    "stock_scanner": "stock_scanner",
    "crypto_scanner": "crypto_scanner",
    "portfolio_monitor": "portfolio_monitor",
    "order_steward": "order_steward_sweep",
    "strategy_coach": "strategy_coach",
    "hold_spy_coordinator": "hold_spy_coordinator",
    "daily_digest": "daily_digest",
    "nightly_review": "nightly_review",
    "calibrator": "calibrate",
    "decision_reflector": "decision_reflect",
    "threshold_tuner": "threshold_tune",
    "debate_outcome_analyzer": "debate_outcome_analyzer",
    "promoter": "auto_promote",
    "param_optimizer": "param_search",
    "intel_ingestor": "intel_ingest_offhours",
    "crypto_intel_ingestor": "crypto_intel_ingestor",
    "crypto_streamer": "crypto_streamer",
    "options_scanner": "options_scanner",
    "position_monitor": "position_monitor",
    "heartbeat": "heartbeat",
    "alert_drain": "alert_drain",
    "schedule_audit": "schedule_audit",
    "log_rotation": "log_rotation",
    "event_bus_retention": "event_bus_retention",
}


# Nodes that aren't BaseRoles (no role_runs row) but have a known
# cron-driven tick recorded in scheduler_last_run.json. Topology lists
# them with role_name=None, so we map node_id → task_id directly to give
# them a heartbeat in the system tab.
_NODE_ID_TASK_ID: dict[str, str] = {
    # Daemon-lifetime nodes: as long as the daemon's heartbeat is ticking,
    # these passive components are alive.
    "scheduler": "heartbeat",
    "process_registry": "heartbeat",
    "cost_tracker": "heartbeat",
    "stall_watchdog": "heartbeat",
    # Audit/QA tasks with their own cron.
    "freshness_audit": "schedule_audit",
    # Weekly architect run.
    "strategy_architect": "saturday_evolve",
    # NOTE: trade_journal and regime_detector are pure in-process
    # components without a scheduler tick. They stay "off" (passive
    # render) instead of being false-aliased to stock_scanner.
}


def _expected_interval_seconds(cron: str) -> int | None:
    """Estimate the expected gap between fires for a 5-field cron.
    Returns None when the schedule is too sparse to estimate (weekly+).
    """
    try:
        from croniter import croniter
    except Exception:
        return None
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        now = datetime.now(et)
        it = croniter(cron, now)
        a = it.get_next(datetime)
        b = it.get_next(datetime)
        delta = (b - a).total_seconds()
        return int(delta) if delta > 0 else None
    except Exception:
        return None


def _seconds_until_next_fire(cron: str) -> int | None:
    """Seconds from *now* until the next time this cron fires. Used to
    soften the failure-state for jobs that are gated to weekday market
    hours: on Sunday at 21:00, a `0 9 * * 1-5` job's "last run" is
    naturally 2d old, but the next fire is ~12h away — the job isn't
    failing, it's *between fires*.
    """
    try:
        from croniter import croniter
        from zoneinfo import ZoneInfo
    except Exception:
        return None
    try:
        et = ZoneInfo("America/New_York")
        now = datetime.now(et)
        it = croniter(cron, now)
        nxt = it.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=et)
        return int((nxt - now).total_seconds())
    except Exception:
        return None


def _thresholds_for_interval(interval_s: int | None) -> tuple[int, int, int]:
    """Return (ok, warn, fail) thresholds tuned to the expected fire interval.

    Pattern:
      * ok ≤ interval × 1.5     (one missed beat tolerated)
      * warn ≤ interval × 3
      * fail beyond that

    Floor at the default thresholds so a 60-second cadence doesn't tip
    instantly on a single second of jitter.
    """
    if interval_s is None or interval_s <= 0:
        return _DEFAULT_OK_AGE_S, _DEFAULT_WARN_AGE_S, _DEFAULT_FAIL_AGE_S
    ok = max(_DEFAULT_OK_AGE_S, int(interval_s * 1.5))
    warn = max(_DEFAULT_WARN_AGE_S, int(interval_s * 3.0))
    fail = max(_DEFAULT_FAIL_AGE_S, int(interval_s * 6.0))
    return ok, warn, fail


def _relative_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _classify(age_s: int | None, thresholds: tuple[int, int, int] | None = None) -> str:
    if age_s is None:
        return "off"
    ok_s, warn_s, fail_s = thresholds or (
        _DEFAULT_OK_AGE_S, _DEFAULT_WARN_AGE_S, _DEFAULT_FAIL_AGE_S,
    )
    if age_s <= ok_s:
        return "ok"
    if age_s <= warn_s:
        return "warn"
    if age_s <= fail_s:
        return "warn"  # still warn — fail only on actual error rows
    return "fail"


def _load_scheduler_history() -> dict[str, datetime]:
    """Best-effort read of data/scheduler_last_run.json. Returns
    {task_id: aware-UTC datetime}; empty on any failure."""
    path = os.environ.get(
        "TRADING_BOT_SCHED_HISTORY", "data/scheduler_last_run.json",
    )
    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out: dict[str, datetime] = {}
    for task, ts in raw.items():
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[task] = dt
        except Exception:
            continue
    return out


def _cron_by_task_id() -> dict[str, str]:
    """Lazy-load the cron expression for each scheduled job so we can
    derive cadence-aware thresholds without importing data.py (which
    pulls in the whole dashboard build path)."""
    try:
        from trading_bot.dashboard.data import _KNOWN_SCHEDULED_JOBS
        return {task_id: cron for (task_id, _label, cron) in _KNOWN_SCHEDULED_JOBS}
    except Exception:
        return {}


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
    sched_history = _load_scheduler_history()
    cron_by_id = _cron_by_task_id()
    # Cache derived thresholds per-cron so we don't re-evaluate croniter
    # for every node on every snapshot build.
    _thresh_cache: dict[str, tuple[int, int, int]] = {}

    def _node_cron(node_id: str, role_name: str | None) -> str | None:
        if role_name:
            task_id = _TASK_ID_ALIAS.get(role_name, role_name)
        else:
            task_id = _NODE_ID_TASK_ID.get(node_id)
        if not task_id:
            return None
        return cron_by_id.get(task_id)

    def _node_thresholds(node_id: str, role_name: str | None) -> tuple[int, int, int]:
        cron = _node_cron(node_id, role_name)
        if not cron:
            return _DEFAULT_OK_AGE_S, _DEFAULT_WARN_AGE_S, _DEFAULT_FAIL_AGE_S
        cached = _thresh_cache.get(cron)
        if cached is not None:
            return cached
        interval = _expected_interval_seconds(cron)
        thresholds = _thresholds_for_interval(interval)
        _thresh_cache[cron] = thresholds
        return thresholds

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
            # 3. Fallback to scheduler_last_run.json for cron-driven jobs
            #    (heartbeat, alert_drain, log_rotation, …) that don't write
            #    role_runs but the scheduler tracks.
            role_ts: datetime | None = None
            role_status: str | None = None
            ev_ts: datetime | None = None
            sched_ts: datetime | None = None
            if n.role_name:
                role_ts, role_status = _last_role_run(conn, n.role_name)
                # Use scheduler_history only as a *fallback* when role_runs
                # has no entry. role_runs is the higher-fidelity source
                # (timestamps to the second; carries ok/error status), so
                # we don't let an older sched_ts override a fresh role_ts.
                if role_ts is None:
                    task_id = _TASK_ID_ALIAS.get(n.role_name, n.role_name)
                    sched_ts = sched_history.get(task_id)
            else:
                # Non-BaseRole nodes (scheduler, freshness_audit, …) can
                # still get a heartbeat from a scheduler tick if we've
                # mapped them above.
                task_id = _NODE_ID_TASK_ID.get(n.id)
                if task_id:
                    sched_ts = sched_history.get(task_id)
            if n.subscribes:
                ev_ts = _last_event_ts(conn, n.subscribes)

            ts = max(
                (t for t in (role_ts, ev_ts, sched_ts) if t is not None),
                default=None,
            )
            if ts is None:
                health = "off"
            elif role_status == "error" and role_ts is not None and (
                (ev_ts is None or ev_ts <= role_ts)
                and (sched_ts is None or sched_ts <= role_ts)
            ):
                # Most-recent signal is an errored role run.
                health = "fail"
            else:
                age_s = int((now - ts).total_seconds())
                health = _classify(age_s, _node_thresholds(n.id, n.role_name))
                # Soften failure when the job is between fires. Many
                # weekday-only crons look 2d stale at 9 PM Sunday — but
                # next fire is Monday morning, so the job isn't failing,
                # it's *waiting*. Cap at "warn" until we're meaningfully
                # past the expected next fire.
                if health == "fail":
                    cron = _node_cron(n.id, n.role_name)
                    if cron:
                        until_next = _seconds_until_next_fire(cron)
                        # Future fire ahead with reasonable lead time → warn,
                        # not fail. 30-min grace keeps us from oscillating
                        # the instant the cron crosses zero.
                        if until_next is not None and until_next > 30 * 60:
                            health = "warn"

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
