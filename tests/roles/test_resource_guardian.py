# tests/roles/test_resource_guardian.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.resource_guardian import ResourceGuardianRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = ResourceGuardianRole(
        engine=None, repo_root="/tmp", state_db_path="/tmp/state.db",
        journal_db_path="/tmp/journal.db",
    )
    assert role.name == "resource_guardian"
    assert role.process == "supervisor"
    assert role.tier == 6


def test_safe_run_returns_disk_db_metrics(engine, tmp_path):
    state_db = tmp_path / "state.db"
    state_db.write_bytes(b"x" * 1024)
    journal_db = tmp_path / "journal.db"
    journal_db.write_bytes(b"x" * 2048)
    role = ResourceGuardianRole(
        engine=engine, repo_root=tmp_path,
        state_db_path=state_db, journal_db_path=journal_db,
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["disk_free_gb"] > 0
    assert result.outputs["state_db_mb"] >= 0
    assert result.outputs["journal_db_mb"] >= 0


def test_warns_when_disk_below_threshold(engine, tmp_path):
    role = ResourceGuardianRole(
        engine=engine, repo_root=tmp_path,
        state_db_path=tmp_path / "state.db", journal_db_path=tmp_path / "journal.db",
        disk_warn_gb=10**9,  # huge — guaranteed to trip on a normal Mac
    )
    result = role.safe_run(ctx={})
    assert "disk_low_gb" in result.outputs.get("warnings", [])
