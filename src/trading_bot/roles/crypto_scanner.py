# src/trading_bot/roles/crypto_scanner.py
"""Crypto Scanner — runs ``crypto-scan`` every 30 min, 24/7.

Phase 1G.3 — closes the bypass: ``cli.crypto_scan`` now prefers
scout-elevated candidates from ``intel_candidates_crypto`` (those that
passed Sasha/Lena/Diane's two-call debate), falling back to the manual
Alpaca crypto universe when no scout-elevated candidates exist.

The role itself is unchanged — the scout-aware behavior lives in
``cli.crypto_scan`` so a single point of change covers both the daemon
role AND ad-hoc CLI invocations.
"""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class CryptoScannerRole(BaseRole):
    name = "crypto_scanner"
    tier = 2
    process = "daemon"
    job_description = (
        "Run crypto-scan every 30 min, 24/7. Reads scout-elevated crypto "
        "candidates from intel_candidates_crypto when available; falls back "
        "to the manual Alpaca crypto universe. Sentiment floor is not applied "
        "to crypto. Runs through Risk Officer + Trade Executor."
    )
    sla_seconds = 60
    upstream_roles: list[str] = ["intel_ingestor"]   # scout debate must run first
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
