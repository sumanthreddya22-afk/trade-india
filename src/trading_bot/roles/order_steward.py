# src/trading_bot/roles/order_steward.py
"""Order Steward — Tier 3. Post-order lifecycle: verify fills, ensure stops
attached, cancel stale unfilled limit orders."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class OrderStewardRole(BaseRole):
    name = "order_steward"
    tier = 3
    process = "daemon"
    job_description = (
        "Verify every open position has a live stop order. Cancel "
        "unfilled limit orders older than 60 min. Sweeps every 60 min "
        "during market hours plus immediate on-demand after each Trade "
        "Executor placement."
    )
    sla_seconds = 60
    upstream_roles = ["trade_executor"]
    downstream_roles = ["reporter"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.verify_stops.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("stop_attached_rate", 1.0, "Phase 3 KPI activates with positions table")
