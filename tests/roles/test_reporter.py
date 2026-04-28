# tests/roles/test_reporter.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.reporter import ReporterRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = ReporterRole(engine=None)
    assert role.name == "reporter"
    assert role.tier == 6


def test_run_eod_invokes_eod_report(engine):
    role = ReporterRole(engine=engine)
    with patch("trading_bot.cli.eod_report") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.run_eod(ctx={})
    assert result.status == RoleStatus.OK


def test_run_midday_invokes_rich_report_mid(engine):
    role = ReporterRole(engine=engine)
    with patch("trading_bot.cli.rich_report") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.run_midday(ctx={})
        mc.callback.assert_called_with(period="mid")
    assert result.status == RoleStatus.OK
