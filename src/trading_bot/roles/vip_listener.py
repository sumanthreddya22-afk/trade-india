# src/trading_bot/roles/vip_listener.py
"""VIP Listener — Tier 1. Polls Truth Social RSS, flags HIGH-severity posts.
Alert-only — never trades."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class VipListenerRole(BaseRole):
    name = "vip_listener"
    tier = 1
    process = "daemon"
    job_description = (
        "Poll Truth Social RSS every 30 min during US market hours, flag "
        "HIGH-severity posts. Alert-only — never auto-trades, auto-halts, "
        "or auto-vetoes based on a tweet."
    )
    sla_seconds = 30
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.vip_scan.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("alerts_per_week", 0.0, "Phase 3+ — needs alerts table")
