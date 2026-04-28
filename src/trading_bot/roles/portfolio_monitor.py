# src/trading_bot/roles/portfolio_monitor.py
"""Portfolio Monitor — Tier 4 stewardship. Snapshots positions every 60min
during market hours, alerts on stop-hits, big moves, unusual fills."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class PortfolioMonitorRole(BaseRole):
    name = "portfolio_monitor"
    tier = 4
    process = "daemon"
    job_description = (
        "Snapshot Alpaca positions every 60 min during market hours. "
        "Alert on stop-hits, big intraday moves, unusual fills. "
        "Stop-hit emails are routed via Trade Executor's fill detection."
    )
    sla_seconds = 30
    upstream_roles = ["trade_executor"]
    downstream_roles = ["reporter"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.portfolio_watch.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("alert_lead_time_seconds", 0.0, "Phase 3 KPI (needs alert events table)")
