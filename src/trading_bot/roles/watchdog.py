# src/trading_bot/roles/watchdog.py
"""Watchdog — Tier 6 supervisor role. Detects daemon stall via heartbeat
mtime, attempts launchctl kickstart. The cooldown for the alert email
itself lives in supervisor.py's _send_alert (Phase 1)."""
from __future__ import annotations
from pathlib import Path
from trading_bot.roles.runner import BaseRole
from trading_bot.watchdog_stall import StallDetector


class WatchdogRole(BaseRole):
    name = "watchdog"
    tier = 6
    process = "supervisor"
    job_description = (
        "Detect daemon stall via heartbeat staleness > max_age_seconds. "
        "On stall, attempt one launchctl kickstart of the daemon plist. "
        "Caller (supervisor main loop) emits the alert email."
    )
    sla_seconds = 5
    upstream_roles = ["health_pulse"]
    downstream_roles: list[str] = []

    def __init__(self, *, engine, heartbeat_path: str | Path,
                 max_age_seconds: int, plist_label: str):
        super().__init__(engine=engine)
        self.detector = StallDetector(
            heartbeat_path=heartbeat_path,
            max_age_seconds=max_age_seconds,
            plist_label=plist_label,
        )

    def _do_work(self, ctx):
        verdict = self.detector.check()
        out = {"stalled": verdict.is_stalled, "age_seconds": verdict.age_seconds}
        if verdict.is_stalled:
            out["kickstart_attempted"] = self.detector.kickstart_daemon()
        return out

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # # of successful kickstarts in lookback period (from role_runs.outputs).
        # Phase 2 placeholder — Phase 3 will add an outputs JSON column or
        # parse from a separate events table.
        return (
            "kickstart_count",
            0.0,
            "Phase 3 KPI; Phase 2 placeholder",
        )
