# tests/roles/test_vip_listener.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.vip_listener import VipListenerRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = VipListenerRole(engine=None)
    assert role.name == "vip_listener"
    assert role.tier == 1
    assert "alert" in role.job_description.lower()


def test_do_work_invokes_vip_scan(engine):
    role = VipListenerRole(engine=engine)
    with patch("trading_bot.cli.vip_scan") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = VipListenerRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "alerts_per_week"
