# src/trading_bot/roles/account_sentinel.py
"""Account Sentinel — Tier 6 supervisor role. Independently fetches Alpaca
equity, updates HWM, computes drawdown vs HWM, writes pause.flag if breached.
Wraps existing AccountSentinel class from watchdog_account.py."""
from __future__ import annotations
from pathlib import Path
from trading_bot.roles.runner import BaseRole
from trading_bot.watchdog_account import AccountSentinel


class AccountSentinelRole(BaseRole):
    name = "account_sentinel"
    tier = 6
    process = "supervisor"
    job_description = (
        "Reconcile Alpaca account vs trade journal independently of daemon. "
        "Update equity HWM, compute drawdown, write pause.flag if drawdown "
        "exceeds max_dd_pct. Runs every 5 min during market hours, every "
        "30 min off-hours."
    )
    sla_seconds = 30
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    def __init__(self, *, engine, alpaca, pause_flag_path: str | Path,
                 max_dd_pct: float, account: str):
        super().__init__(engine=engine)
        self.sentinel = AccountSentinel(
            engine=engine, alpaca=alpaca,
            pause_flag_path=pause_flag_path,
            max_dd_pct=max_dd_pct, account=account,
        )

    def _do_work(self, ctx):
        verdict = self.sentinel.check()
        return {
            "equity": str(verdict.equity),
            "hwm": verdict.hwm,
            "drawdown_pct": verdict.drawdown_pct,
            "paused": verdict.paused,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return (
            "current_drawdown_pct",
            0.0,
            "Phase 3 KPI; Phase 2 reports current snapshot only",
        )
