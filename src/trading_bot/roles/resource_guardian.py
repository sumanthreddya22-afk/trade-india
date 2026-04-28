# src/trading_bot/roles/resource_guardian.py
"""Resource Guardian — Tier 6 supervisor role. Tracks disk space, DB sizes,
network connectivity. Anthropic budget tracking added in Phase 5 when the
Strategy Architect role lands."""
from __future__ import annotations
import shutil
import socket
from pathlib import Path
from trading_bot.roles.runner import BaseRole


class ResourceGuardianRole(BaseRole):
    name = "resource_guardian"
    tier = 6
    process = "supervisor"
    job_description = (
        "Track disk free space, SQLite DB sizes, network connectivity to "
        "Alpaca + Polygon. Warn when thresholds tripped. Phase 2 covers "
        "disk + DB + network only; Anthropic budget tracking ships in Phase 5."
    )
    sla_seconds = 10
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    def __init__(self, *, engine, repo_root: str | Path,
                 state_db_path: str | Path, journal_db_path: str | Path,
                 disk_warn_gb: int = 10):
        super().__init__(engine=engine)
        self.repo_root = Path(repo_root)
        self.state_db_path = Path(state_db_path)
        self.journal_db_path = Path(journal_db_path)
        self.disk_warn_gb = disk_warn_gb

    def _do_work(self, ctx):
        warnings = []
        # Disk
        usage = shutil.disk_usage(self.repo_root)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < self.disk_warn_gb:
            warnings.append("disk_low_gb")

        # DB sizes
        state_mb = (
            self.state_db_path.stat().st_size / (1024 ** 2)
            if self.state_db_path.exists() else 0.0
        )
        journal_mb = (
            self.journal_db_path.stat().st_size / (1024 ** 2)
            if self.journal_db_path.exists() else 0.0
        )

        # Network probes (don't fail the run on unreachable endpoints)
        alpaca_reachable = self._reachable("api.alpaca.markets", 443)
        polygon_reachable = self._reachable("api.polygon.io", 443)
        if not alpaca_reachable:
            warnings.append("alpaca_unreachable")
        if not polygon_reachable:
            warnings.append("polygon_unreachable")

        return {
            "disk_free_gb": round(free_gb, 2),
            "state_db_mb": round(state_mb, 2),
            "journal_db_mb": round(journal_mb, 2),
            "alpaca_reachable": alpaca_reachable,
            "polygon_reachable": polygon_reachable,
            "warnings": warnings,
        }

    def _reachable(self, host: str, port: int, timeout_seconds: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                return True
        except (OSError, socket.timeout):
            return False

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        outputs = self._do_work(ctx=None)
        return (
            "disk_free_gb",
            outputs["disk_free_gb"],
            f"{outputs['disk_free_gb']} GB free; warnings: {outputs.get('warnings', [])}",
        )
