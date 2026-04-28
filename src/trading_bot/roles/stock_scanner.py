"""Stock Scanner — runs intel-scan during US market hours, emits BUY/HOLD/SKIP
per stock candidate. Tier 2 (decision making). Wraps the existing
cli.intel_scan command.

KPI: buy_win_rate_5d — % of BUY decisions whose 5-day forward return is
positive. Computed by joining trade_journal.decisions (or fills) against
the next 5 trading days' bars.
"""
from __future__ import annotations

from trading_bot.roles.runner import BaseRole


class StockScannerRole(BaseRole):
    name = "stock_scanner"
    tier = 2
    process = "daemon"
    job_description = (
        "Run hourly intel-scan during US market hours. Evaluate stage-2 "
        "watchlist, emit BUY/HOLD/SKIP per candidate. Never places orders "
        "directly — Risk Officer + Trade Executor handle that."
    )
    sla_seconds = 60
    upstream_roles = ["universe_curator", "sentiment_analyst"]
    downstream_roles = ["risk_officer", "trade_executor"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.intel_scan.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # KPI computation deferred to Phase 3 (when trade_journal has fills).
        # Phase 2 reports a placeholder so the report card has structure;
        # Phase 3 will replace this with a real win-rate query.
        return (
            "buy_win_rate_5d",
            0.0,
            "no buys in window (KPI activates in Phase 3 once journal accrues)",
        )
