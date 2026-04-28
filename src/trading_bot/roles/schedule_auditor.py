# src/trading_bot/roles/schedule_auditor.py
"""Schedule Auditor — Tier 6 supervisor role. Verifies every expected role
ran within its grace window. Reports missed roles. Daily roll-up at 17:00 ET
caught by supervisor's main loop and emailed via Reporter (Phase 3 wiring).
"""
from __future__ import annotations
import datetime as dt
from sqlalchemy import desc
from sqlalchemy.orm import Session
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun


class ScheduleAuditorRole(BaseRole):
    name = "schedule_auditor"
    tier = 6
    process = "supervisor"
    job_description = (
        "Verify every expected role ran within its grace window. Reports "
        "missed roles. Daily roll-up at 17:00 ET surfaces in the digest."
    )
    sla_seconds = 5
    upstream_roles: list[str] = []
    downstream_roles = ["reporter"]

    # Map of role_name → grace window in seconds (3x the cadence + some slack).
    # Roles whose schedule is mkt-hours-only are also checked but tolerated
    # outside market hours by adding the >24h grace in the supervisor.
    EXPECTED_ROLES: dict[str, int] = {
        "health_pulse": 180,           # heartbeat every 60s; allow 3x
        "stock_scanner": 4 * 3600,     # mkt-hours hourly; gives ~4h grace
        "crypto_scanner": 90 * 60,     # 24/7 every 30min; allow 3x
        "portfolio_monitor": 4 * 3600,
        "order_steward": 4 * 3600,
        "vip_listener": 2 * 3600,
        "sentiment_analyst": 8 * 3600, # twice daily
        "reporter": 30 * 3600,         # twice daily but spaced widely
        "watchdog": 180,
        "account_sentinel": 30 * 60,   # market-hours every 5min, off-hours 30min
    }

    def _do_work(self, ctx):
        now = dt.datetime.now(dt.timezone.utc)
        missed = []
        with Session(self.engine) as session:
            for role_name, grace_seconds in self.EXPECTED_ROLES.items():
                latest = (
                    session.query(RoleRun)
                    .filter(RoleRun.role_name == role_name)
                    .order_by(desc(RoleRun.started_at))
                    .first()
                )
                if latest is None:
                    missed.append(role_name)
                    continue
                last_started = latest.started_at
                # SQLite returns naive datetimes even for timezone=True columns.
                if last_started.tzinfo is None:
                    last_started = last_started.replace(tzinfo=dt.timezone.utc)
                age = (now - last_started).total_seconds()
                if age > grace_seconds:
                    missed.append(role_name)
        return {"missed": missed, "checked": list(self.EXPECTED_ROLES.keys())}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            rows = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .all()
            )
        if not rows:
            return ("missed_role_rate", 0.0, "no audits yet")
        # Approximate: this requires storing outputs.missed in role_runs which Phase 2 doesn't do.
        # Phase 2 reports zero; Phase 3 adds outputs storage.
        return (
            "missed_role_rate",
            0.0,
            "Phase 3 KPI; Phase 2 placeholder",
        )
