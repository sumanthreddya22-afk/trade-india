"""Health Pulse — daemon's heartbeat as a Role.

Tier 6 (Supervision/observability). Runs every cadence.heartbeat_seconds
inside the daemon process. The supervisor reads the heartbeat file mtime
to detect stalls. Charter intentionally minimal: just keep the pulse alive.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun
from trading_bot.state_heartbeat import write_heartbeat


class HealthPulseRole(BaseRole):
    name = "health_pulse"
    tier = 6
    process = "daemon"
    job_description = (
        "Write daemon heartbeat to disk every cadence.heartbeat_seconds. "
        "Supervisor reads mtime to detect stalls."
    )
    sla_seconds = 5
    upstream_roles: list[str] = []
    downstream_roles = ["watchdog"]

    def __init__(self, *, engine, heartbeat_path: str | Path, version: str):
        super().__init__(engine=engine)
        self.heartbeat_path = Path(heartbeat_path)
        self.version = version

    def _do_work(self, ctx):
        write_heartbeat(self.heartbeat_path, version=self.version, last_action="heartbeat")
        return {"path": str(self.heartbeat_path)}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        per_day = count / max(lookback_days, 1)
        return (
            "heartbeats_per_day",
            per_day,
            f"{count} heartbeats in last {lookback_days}d ({per_day:.0f}/day)",
        )
