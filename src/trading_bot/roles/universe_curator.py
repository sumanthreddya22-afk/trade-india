# src/trading_bot/roles/universe_curator.py
"""Universe Curator — Tier 1. Maintains the tradable list and the cached
daily bars. Two sub-jobs: refresh (06:30 ET, pulls Polygon grouped bars)
and rank (07:30 ET, runs stage-1+2 screener)."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole
from trading_bot.roles.base import RoleResult


class UniverseCuratorRole(BaseRole):
    name = "universe_curator"
    tier = 1
    process = "daemon"
    job_description = (
        "Maintain the tradable stock list. Refresh Polygon grouped daily "
        "bars (06:30 ET); rank stage-1+2 candidates into top 25 (07:30 ET)."
    )
    sla_seconds = 120
    upstream_roles: list[str] = []
    downstream_roles = ["stock_scanner", "sentiment_analyst"]

    def run_refresh(self, ctx) -> RoleResult:
        return self._run_subjob("refresh")

    def run_rank(self, ctx) -> RoleResult:
        return self._run_subjob("rank")

    def _run_subjob(self, job: str) -> RoleResult:
        # Reuses safe_run with a contextual override
        self._current_subjob = job
        try:
            return self.safe_run(ctx={"subjob": job})
        finally:
            self._current_subjob = None

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        job = (ctx or {}).get("subjob") or getattr(self, "_current_subjob", None) or "refresh"
        if job == "refresh":
            cli_mod.massive_refresh.callback(days=5, news=False)
        elif job == "rank":
            cli_mod.rank_command.callback()
        else:
            raise ValueError(f"unknown universe_curator subjob: {job}")
        return {"job": job}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # Phase 3+ will compute capture rate from journal vs. the next-day winners.
        return (
            "top25_capture_rate_14d",
            0.0,
            "KPI activates in Phase 3 (requires journal of placed BUYs to compare against next-day winners)",
        )
