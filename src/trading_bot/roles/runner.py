"""BaseRole — concrete base class implementing the Role Protocol with
safe_run + KPI persistence. Subclasses override _do_work() and _kpi_value().

safe_run is the gate that catches every exception (including BaseException)
so APScheduler worker threads and the supervisor loop never die from a
buggy role.
"""
from __future__ import annotations

import datetime as dt
import time as _time
import traceback

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.roles.base import (
    Health,
    HealthStatus,
    ReportCard,
    RoleResult,
    RoleStatus,
)
from trading_bot.state_db import RoleKpi, RoleRun


class BaseRole:
    """Concrete implementation of the Role Protocol. Subclasses override
    `_do_work(ctx)` (the actual work) and `_kpi_value(lookback_days)`
    (returns a (kpi_name, value, summary) tuple).
    """

    name: str = "base"
    tier: int = 0
    process: str = "daemon"
    job_description: str = "base role — do not instantiate"
    sla_seconds: int = 60
    upstream_roles: list[str] = []
    downstream_roles: list[str] = []

    def __init__(self, *, engine):
        self.engine = engine

    def _do_work(self, ctx):
        raise NotImplementedError("subclasses must override _do_work")

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        """Return (kpi_name, value, prose summary)."""
        raise NotImplementedError("subclasses must override _kpi_value")

    def safe_run(self, ctx) -> RoleResult:
        started = dt.datetime.now(dt.timezone.utc)
        t0 = _time.monotonic()
        outputs: dict = {}
        status = RoleStatus.OK
        error_text: str | None = None

        try:
            outputs = self._do_work(ctx) or {}
        except BaseException as e:  # catch SystemExit too — workers must survive
            status = RoleStatus.ERROR
            error_text = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        finally:
            finished = dt.datetime.now(dt.timezone.utc)
            latency_ms = int((_time.monotonic() - t0) * 1000)

        result = RoleResult(
            role_name=self.name,
            started_at=started,
            finished_at=finished,
            status=status,
            latency_ms=latency_ms,
            outputs=outputs,
            error_text=error_text,
        )
        self._persist_run(result)
        return result

    def _persist_run(self, result: RoleResult) -> None:
        with Session(self.engine) as session:
            row = RoleRun(
                role_name=result.role_name,
                started_at=result.started_at,
                finished_at=result.finished_at,
                status=result.status.value,
                latency_ms=result.latency_ms,
                error_text=result.error_text,
            )
            session.add(row)
            session.commit()

    def persist_kpi(self, lookback_days: int = 30) -> None:
        kpi_name, value, _ = self._kpi_value(lookback_days)
        with Session(self.engine) as session:
            row = RoleKpi(
                role_name=self.name,
                kpi_name=kpi_name,
                value=value,
                recorded_at=dt.datetime.now(dt.timezone.utc),
            )
            session.add(row)
            session.commit()

    def report_card(self, lookback_days: int = 30) -> ReportCard:
        kpi_name, value, summary = self._kpi_value(lookback_days)
        delta = self._prior_period_delta(kpi_name, lookback_days, value)
        health = self.health_check()
        return ReportCard(
            role_name=self.name,
            period_days=lookback_days,
            kpi_name=kpi_name,
            kpi_value=value,
            delta_vs_prior=delta,
            summary=summary,
            health=health.status,
        )

    def _prior_period_delta(self, kpi_name: str, lookback_days: int, current: float) -> float | None:
        """Look up the most recent KPI row > lookback_days old and return current - prior."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            row = (
                session.query(RoleKpi)
                .filter(RoleKpi.role_name == self.name, RoleKpi.kpi_name == kpi_name)
                .filter(RoleKpi.recorded_at < cutoff)
                .order_by(desc(RoleKpi.recorded_at))
                .first()
            )
        return current - row.value if row else None

    def health_check(self) -> Health:
        """Default: DEGRADED if > 30% of the last 10 runs errored, else OK."""
        with Session(self.engine) as session:
            runs = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name)
                .order_by(desc(RoleRun.started_at))
                .limit(10)
                .all()
            )
        if not runs:
            return Health(status=HealthStatus.OK, detail="no runs yet")
        errors = sum(1 for r in runs if r.status == "error")
        if errors / len(runs) > 0.30:
            return Health(
                status=HealthStatus.DEGRADED,
                detail=f"{errors} of last {len(runs)} runs errored",
            )
        return Health(status=HealthStatus.OK)

    def run(self, ctx) -> RoleResult:
        """Protocol method — alias for safe_run so BaseRole satisfies Role Protocol."""
        return self.safe_run(ctx)
