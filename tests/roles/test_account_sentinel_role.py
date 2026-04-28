# tests/roles/test_account_sentinel_role.py
import os, tempfile
from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.account_sentinel import AccountSentinelRole


@pytest.fixture
def engine():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{p}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(p)


def test_charter():
    role = AccountSentinelRole(
        engine=None, alpaca=MagicMock(), pause_flag_path="/tmp/x",
        max_dd_pct=20.0, account="paper",
    )
    assert role.name == "account_sentinel"
    assert role.process == "supervisor"
    assert role.tier == 6


def test_safe_run_returns_drawdown(engine, tmp_path):
    alpaca = MagicMock()
    alpaca.get_account.return_value = MagicMock(equity=Decimal("100000"))
    role = AccountSentinelRole(
        engine=engine, alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0, account="paper",
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert "drawdown_pct" in result.outputs
    assert result.outputs["drawdown_pct"] == 0.0
    assert result.outputs["paused"] is False


def test_safe_run_handles_alpaca_failure(engine, tmp_path):
    alpaca = MagicMock()
    alpaca.get_account.side_effect = ConnectionError("alpaca down")
    role = AccountSentinelRole(
        engine=engine, alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0, account="paper",
    )
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
