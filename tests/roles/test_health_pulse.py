import json
import os
import tempfile
import datetime as dt

import pytest
from sqlalchemy import create_engine

from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus, HealthStatus
from trading_bot.roles.health_pulse import HealthPulseRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_health_pulse_writes_heartbeat(engine, tmp_path):
    hb_path = tmp_path / "heartbeat.json"
    role = HealthPulseRole(engine=engine, heartbeat_path=hb_path, version="phase2-v1")
    result = role.safe_run(ctx=None)
    assert result.status == RoleStatus.OK
    assert hb_path.exists()
    payload = json.loads(hb_path.read_text())
    assert payload["version"] == "phase2-v1"
    assert payload["last_action"] == "heartbeat"


def test_health_pulse_charter():
    role = HealthPulseRole(engine=None, heartbeat_path="/tmp/x", version="v1")
    assert role.name == "health_pulse"
    assert role.process == "daemon"
    assert role.tier == 6
    assert "heartbeat" in role.job_description.lower()


def test_health_pulse_kpi(engine, tmp_path):
    role = HealthPulseRole(engine=engine, heartbeat_path=tmp_path / "hb.json", version="v1")
    role.safe_run(ctx=None)
    name, value, summary = role._kpi_value(lookback_days=1)
    assert name == "heartbeats_per_day"
    assert value >= 1
