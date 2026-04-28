# tests/roles/test_portfolio_monitor.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.portfolio_monitor import PortfolioMonitorRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = PortfolioMonitorRole(engine=None)
    assert role.name == "portfolio_monitor"
    assert role.tier == 4


def test_do_work_invokes_portfolio_watch(engine):
    role = PortfolioMonitorRole(engine=engine)
    with patch("trading_bot.cli.portfolio_watch") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = PortfolioMonitorRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "alert_lead_time_seconds"
