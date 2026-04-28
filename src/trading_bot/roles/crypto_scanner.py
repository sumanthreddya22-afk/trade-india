# src/trading_bot/roles/crypto_scanner.py
"""Crypto Scanner — same as StockScannerRole but for crypto pairs, 24/7,
no sentiment floor. Tier 2."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class CryptoScannerRole(BaseRole):
    name = "crypto_scanner"
    tier = 2
    process = "daemon"
    job_description = (
        "Run crypto-scan every 30 min, 24/7. Evaluates configured crypto "
        "pairs (BTC/USD, ETH/USD, SOL/USD by default). Sentiment floor "
        "is not applied to crypto. Runs through Risk Officer + Trade Executor."
    )
    sla_seconds = 60
    upstream_roles: list[str] = []
    downstream_roles = ["risk_officer", "trade_executor"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        from trading_bot.state_fallback import is_fallback_active

        if is_fallback_active(self.engine):
            return {"skipped": True, "reason": "fallback_active"}
        cli_mod.crypto_scan.callback()
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        return ("buy_win_rate_5d", 0.0, "Phase 3 KPI — placeholder")
