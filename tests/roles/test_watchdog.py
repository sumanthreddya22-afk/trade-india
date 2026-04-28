# tests/roles/test_watchdog.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.watchdog import WatchdogRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = WatchdogRole(
        engine=None,
        heartbeat_path="/tmp/x",
        max_age_seconds=300,
        plist_label="com.bharath.trading.daemon.paper",
    )
    assert role.name == "watchdog"
    assert role.process == "supervisor"
    assert role.tier == 6


def test_no_stall_when_recent_heartbeat(engine, tmp_path):
    hb = tmp_path / "hb.json"
    hb.write_text('{"ts":"2026-04-28T00:00:00+00:00","pid":1,"version":"v","last_action":"x"}')
    role = WatchdogRole(
        engine=engine, heartbeat_path=hb, max_age_seconds=300,
        plist_label="fake.label",
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["stalled"] is False


def test_stall_triggers_kickstart(engine, tmp_path):
    hb = tmp_path / "hb.json"
    hb.write_text('{}')
    import os as _os
    old = 1234567890
    _os.utime(hb, (old, old))
    role = WatchdogRole(
        engine=engine, heartbeat_path=hb, max_age_seconds=60,
        plist_label="fake.label",
    )
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["stalled"] is True
    assert result.outputs["kickstart_attempted"] is True
