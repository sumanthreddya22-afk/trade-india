"""Drift-prevention test: dashboard's `_KNOWN_SCHEDULED_JOBS` must stay in
sync with the actual jobs the daemon registers.

Background: the daemon registers crons in scheduler_jobs.register_jobs(),
but the dashboard renders them from a hardcoded list in dashboard/data.py.
Those drifted (e.g. daily_digest moved 18:00→16:30 ET in scheduler_jobs.py
but the dashboard kept showing 18:00 for weeks).

This test boots an APScheduler instance, registers all jobs the daemon
would, then asserts every registered job_id appears in the dashboard list.
Triggers reverse direction: every dashboard entry must correspond to a
real registered job — no phantom jobs.

Doesn't compare cron strings byte-for-byte (registered triggers can be
Interval not Cron, and the dashboard accepts cron-string approximations
for IntervalTriggers). Just enforces the SET of job_ids matches.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _registered_job_ids() -> set[str]:
    """Boot register_jobs with stub runners + a real BackgroundScheduler.
    Returns the set of registered job ids."""
    from apscheduler.schedulers.background import BackgroundScheduler

    from trading_bot.cadence import CadenceConfig
    from trading_bot.scheduler_jobs import register_jobs

    # Every runner key the scheduler may attach. Stub each one — we never
    # fire the jobs, only enumerate.
    runner_keys = (
        "heartbeat", "alert_drain", "intel_scan", "crypto_scan",
        "portfolio_watch", "verify_stops", "vip_scan", "news_warm",
        "massive_refresh", "premarket_rank", "midday_rerank",
        "midday_snapshot", "daily_digest", "log_rotation",
        "strategy_coach", "hold_spy_coordinator", "schedule_audit",
        "reconciler",
        "wheel_scan", "wheel_manage", "iv_capture", "wheel_universe_build",
    )
    runners = {k: MagicMock() for k in runner_keys}

    cadence = CadenceConfig()  # defaults are fine
    sched = BackgroundScheduler(timezone="America/New_York")
    register_jobs(scheduler=sched, runners=runners, cadence=cadence)

    return {j.id for j in sched.get_jobs()}


def test_every_registered_job_appears_on_dashboard():
    from trading_bot.dashboard.data import _KNOWN_SCHEDULED_JOBS

    registered = _registered_job_ids()
    dashboard_ids = {row[0] for row in _KNOWN_SCHEDULED_JOBS}

    # Lab jobs are registered by trading_bot.lab.register_lab_jobs() in a
    # different process. The dashboard lists them too, but we don't boot
    # the lab here — exclude lab-only ids from the registered side.
    lab_only = {
        "param_search", "auto_promote", "calibrate", "saturday_evolve",
    }
    registered_minus_lab = registered - lab_only

    missing_from_dashboard = registered_minus_lab - dashboard_ids
    assert not missing_from_dashboard, (
        f"daemon registers these jobs but dashboard doesn't show them: "
        f"{sorted(missing_from_dashboard)}"
    )


def test_every_dashboard_job_corresponds_to_real_job_or_known_lab():
    from trading_bot.dashboard.data import _KNOWN_SCHEDULED_JOBS

    registered = _registered_job_ids()
    dashboard_ids = {row[0] for row in _KNOWN_SCHEDULED_JOBS}

    lab_only = {
        "param_search", "auto_promote", "calibrate", "saturday_evolve",
    }
    phantoms = dashboard_ids - registered - lab_only
    assert not phantoms, (
        f"dashboard shows these jobs but no daemon registration found: "
        f"{sorted(phantoms)}"
    )


def test_daily_digest_cron_is_1630_et():
    """Regression test: catches the specific drift the operator saw —
    dashboard claimed 18:00 ET while real cron is 16:30 ET."""
    from trading_bot.dashboard.data import _KNOWN_SCHEDULED_JOBS

    by_id = {row[0]: row for row in _KNOWN_SCHEDULED_JOBS}
    cron = by_id["daily_digest"][2]
    # cron is "minute hour ..." format
    parts = cron.split()
    assert parts[0] == "30", f"daily_digest minute should be 30, got {parts[0]}"
    assert parts[1] == "16", f"daily_digest hour should be 16 (ET), got {parts[1]}"
