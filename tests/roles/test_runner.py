import os
import tempfile
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base, RoleRun, RoleKpi
from trading_bot.roles.base import RoleStatus, HealthStatus, ReportCard
from trading_bot.roles.runner import BaseRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


class _FakeRole(BaseRole):
    name = "fake_scanner"
    tier = 2
    process = "daemon"
    job_description = "fake test role"
    sla_seconds = 30
    upstream_roles = []
    downstream_roles = []

    def __init__(self, engine, *, raise_on_run: Exception | None = None):
        super().__init__(engine=engine)
        self.raise_on_run = raise_on_run
        self.run_count = 0

    def _do_work(self, ctx):
        self.run_count += 1
        if self.raise_on_run:
            raise self.raise_on_run
        return {"placed": 1, "vetoed": 0}

    def _kpi_value(self, lookback_days):
        return ("test_kpi", 0.42, "test summary")


def test_safe_run_records_ok_result(engine):
    role = _FakeRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs == {"placed": 1, "vetoed": 0}
    assert result.latency_ms >= 0
    assert result.error_text is None

    with Session(engine) as s:
        rows = s.query(RoleRun).all()
    assert len(rows) == 1
    assert rows[0].role_name == "fake_scanner"
    assert rows[0].status == "ok"


def test_safe_run_catches_exception(engine):
    role = _FakeRole(engine=engine, raise_on_run=ValueError("boom"))
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
    assert "ValueError" in result.error_text
    assert "boom" in result.error_text

    with Session(engine) as s:
        rows = s.query(RoleRun).all()
    assert rows[0].status == "error"


def test_safe_run_persists_kpi(engine):
    role = _FakeRole(engine=engine)
    role.safe_run(ctx={})
    role.persist_kpi()  # explicit, called by report_card path

    with Session(engine) as s:
        kpi_rows = s.query(RoleKpi).all()
    assert len(kpi_rows) == 1
    assert kpi_rows[0].kpi_name == "test_kpi"
    assert kpi_rows[0].value == pytest.approx(0.42)


def test_report_card_returns_card(engine):
    role = _FakeRole(engine=engine)
    role.safe_run(ctx={})
    card = role.report_card(lookback_days=30)
    assert isinstance(card, ReportCard)
    assert card.role_name == "fake_scanner"
    assert card.kpi_name == "test_kpi"
    assert card.kpi_value == 0.42
    assert card.period_days == 30


def test_health_check_ok_after_clean_run(engine):
    role = _FakeRole(engine=engine)
    role.safe_run(ctx={})
    health = role.health_check()
    assert health.status == HealthStatus.OK


def test_health_check_degraded_after_recent_errors(engine):
    role = _FakeRole(engine=engine, raise_on_run=ValueError("boom"))
    for _ in range(5):
        role.safe_run(ctx={})
    health = role.health_check()
    assert health.status == HealthStatus.DEGRADED


def test_safe_run_never_raises(engine):
    """safe_run must catch BaseException so APScheduler workers stay alive."""
    class _Suicidal(_FakeRole):
        def _do_work(self, ctx):
            raise SystemExit(1)

    role = _Suicidal(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
    # process did not exit
