# src/trading_bot/roles/reporter.py
"""Reporter — Tier 6. Sends digest emails. Two sub-jobs: midday (12:31 ET
runs rich-report --period=mid which scans + emails) and eod (18:00 ET runs
eod-report which is read-only). Also assembles role report cards for the
daily digest body (see Task 18 for the integration)."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole
from trading_bot.roles.base import RoleResult


class ReporterRole(BaseRole):
    name = "reporter"
    tier = 6
    process = "daemon"
    job_description = (
        "Compose and send digest emails. Mid-day rich report at 12:31 ET "
        "(scans + emails). End-of-day digest at 18:00 ET (read-only summary). "
        "Per-trade fills are routed by Trade Executor through SMTP directly."
    )
    sla_seconds = 60
    upstream_roles = ["account_sentinel"]
    downstream_roles: list[str] = []

    def run_eod(self, ctx) -> RoleResult:
        self._current_subjob = "eod"
        try:
            return self.safe_run(ctx={"subjob": "eod"})
        finally:
            self._current_subjob = None

    def run_midday(self, ctx) -> RoleResult:
        self._current_subjob = "midday"
        try:
            return self.safe_run(ctx={"subjob": "midday"})
        finally:
            self._current_subjob = None

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        job = (ctx or {}).get("subjob") or getattr(self, "_current_subjob", None)
        if job == "eod":
            cli_mod.eod_report.callback()
        elif job == "midday":
            cli_mod.rich_report.callback(period="mid")
        else:
            raise ValueError(f"unknown reporter subjob: {job}")
        return {"job": job}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # On-time delivery rate — Phase 2 placeholder, Phase 3 will compute
        # from role_runs vs expected cron schedule.
        return ("delivered_on_time_rate", 1.0, "Phase 3 KPI; Phase 2 placeholder = 100%")
