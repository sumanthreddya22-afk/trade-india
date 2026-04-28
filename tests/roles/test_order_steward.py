# tests/roles/test_order_steward.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.order_steward import OrderStewardRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = OrderStewardRole(engine=None)
    assert role.name == "order_steward"
    assert role.tier == 3


def test_do_work_invokes_verify_stops(engine):
    role = OrderStewardRole(engine=engine)
    with patch("trading_bot.cli.verify_stops") as mc:
        mc.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK


def test_kpi_default(engine):
    role = OrderStewardRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "stop_attached_rate"
