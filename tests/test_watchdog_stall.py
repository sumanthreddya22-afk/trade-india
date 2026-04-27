import os
import datetime as dt
from unittest.mock import MagicMock, patch
import pytest

from trading_bot.watchdog_stall import StallDetector, StallVerdict
from trading_bot.state_heartbeat import write_heartbeat


@pytest.fixture
def hb_path(tmp_path):
    return tmp_path / "heartbeat.json"


def test_no_stall_when_recent(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    d = StallDetector(heartbeat_path=hb_path, max_age_seconds=300)
    v = d.check()
    assert v.is_stalled is False
    assert v.age_seconds < 5


def test_stall_when_file_old(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    old = dt.datetime.now().timestamp() - 600
    os.utime(hb_path, (old, old))
    d = StallDetector(heartbeat_path=hb_path, max_age_seconds=300)
    v = d.check()
    assert v.is_stalled is True
    assert v.age_seconds >= 600


def test_stall_when_file_missing(hb_path):
    d = StallDetector(heartbeat_path=hb_path, max_age_seconds=300)
    v = d.check()
    assert v.is_stalled is True


def test_kickstart_calls_launchctl(hb_path):
    d = StallDetector(
        heartbeat_path=hb_path,
        max_age_seconds=300,
        plist_label="com.bharath.trading.daemon.paper",
    )
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0)
        ok = d.kickstart_daemon()
    assert ok is True
    args, kwargs = run.call_args
    cmd = args[0]
    assert "launchctl" in cmd
    assert "kickstart" in cmd
    assert "com.bharath.trading.daemon.paper" in " ".join(cmd)


def test_kickstart_returns_false_on_nonzero_exit(hb_path):
    d = StallDetector(
        heartbeat_path=hb_path,
        max_age_seconds=300,
        plist_label="com.bharath.trading.daemon.paper",
    )
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=1)
        ok = d.kickstart_daemon()
    assert ok is False
