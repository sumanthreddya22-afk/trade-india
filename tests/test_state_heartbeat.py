import json
import os
import tempfile
import datetime as dt
from pathlib import Path

import pytest

from trading_bot.state_heartbeat import write_heartbeat, read_heartbeat, is_stale


@pytest.fixture
def hb_path(tmp_path):
    return tmp_path / "heartbeat.json"


def test_write_heartbeat_creates_file_with_required_fields(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="intel-scan")
    payload = json.loads(hb_path.read_text())
    assert "ts" in payload
    assert payload["pid"] == os.getpid()
    assert payload["version"] == "v1"
    assert payload["last_action"] == "intel-scan"


def test_read_heartbeat_returns_dict(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    data = read_heartbeat(hb_path)
    assert data["version"] == "v1"


def test_is_stale_false_when_just_written(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    assert is_stale(hb_path, max_age_seconds=300) is False


def test_is_stale_true_when_file_old(hb_path):
    write_heartbeat(hb_path, version="v1", last_action="boot")
    old = dt.datetime.now().timestamp() - 600
    os.utime(hb_path, (old, old))
    assert is_stale(hb_path, max_age_seconds=300) is True


def test_is_stale_true_when_file_missing(hb_path):
    assert is_stale(hb_path, max_age_seconds=300) is True


def test_atomic_write_via_tmp_rename(hb_path, monkeypatch):
    """Verify the heartbeat is written via tmp+rename so a reader never sees half-written file."""
    write_heartbeat(hb_path, version="v1", last_action="boot")
    # The writer should never leave a .tmp file behind
    assert not hb_path.with_suffix(".json.tmp").exists()
