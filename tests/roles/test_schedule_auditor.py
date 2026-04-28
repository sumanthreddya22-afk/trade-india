# tests/roles/test_schedule_auditor.py
import os, tempfile, datetime as dt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.state_db import Base, RoleRun
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.schedule_auditor import ScheduleAuditorRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def _add_run(engine, role_name, started):
    with Session(engine) as s:
        s.add(RoleRun(
            role_name=role_name, started_at=started,
            finished_at=started + dt.timedelta(seconds=1),
            status="ok", latency_ms=1000,
        ))
        s.commit()


def test_charter():
    role = ScheduleAuditorRole(engine=None)
    assert role.name == "schedule_auditor"
    assert role.process == "supervisor"


def test_no_misses_when_all_ran_recently(engine):
    now = dt.datetime.now(dt.timezone.utc)
    for role_name in ScheduleAuditorRole.EXPECTED_ROLES.keys():
        _add_run(engine, role_name, now - dt.timedelta(seconds=30))
    role = ScheduleAuditorRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["missed"] == []


def test_detects_missing_role(engine):
    now = dt.datetime.now(dt.timezone.utc)
    # Only health_pulse ran recently; everything else is missing
    _add_run(engine, "health_pulse", now - dt.timedelta(seconds=30))
    role = ScheduleAuditorRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert "stock_scanner" in result.outputs["missed"] or len(result.outputs["missed"]) > 0
