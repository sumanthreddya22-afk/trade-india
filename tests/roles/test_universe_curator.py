# tests/roles/test_universe_curator.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.state_db import Base, RoleRun
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.universe_curator import UniverseCuratorRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = UniverseCuratorRole(engine=None)
    assert role.name == "universe_curator"
    assert role.tier == 1


def test_run_refresh_invokes_massive_refresh(engine):
    role = UniverseCuratorRole(engine=engine)
    with patch("trading_bot.cli.massive_refresh") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.run_refresh(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK
    with Session(engine) as s:
        row = s.query(RoleRun).first()
    assert row.role_name == "universe_curator"


def test_run_rank_invokes_rank_command(engine):
    role = UniverseCuratorRole(engine=engine)
    with patch("trading_bot.cli.rank_command") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.run_rank(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = UniverseCuratorRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=14)
    assert name == "top25_capture_rate_14d"
