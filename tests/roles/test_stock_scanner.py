import os
import tempfile
import datetime as dt
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine

from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus, HealthStatus
from trading_bot.roles.stock_scanner import StockScannerRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = StockScannerRole(engine=None)
    assert role.name == "stock_scanner"
    assert role.tier == 2
    assert role.process == "daemon"
    assert role.sla_seconds >= 30
    assert "intel-scan" in role.job_description.lower() or "scan" in role.job_description.lower()


def test_do_work_invokes_intel_scan(engine):
    role = StockScannerRole(engine=engine)
    with patch("trading_bot.cli.intel_scan") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK


def test_do_work_handles_exception(engine):
    role = StockScannerRole(engine=engine)
    with patch("trading_bot.cli.intel_scan") as mock_cmd:
        mock_cmd.callback.side_effect = RuntimeError("alpaca down")
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR
    assert "alpaca down" in result.error_text


def test_kpi_returns_buy_win_rate_with_no_trades(engine):
    role = StockScannerRole(engine=engine)
    name, value, summary = role._kpi_value(lookback_days=30)
    assert name == "buy_win_rate_5d"
    assert value == 0.0  # default when no trades
    assert "no buys" in summary.lower() or "0 buys" in summary.lower()


def test_short_circuits_when_fallback_active(engine):
    """Phase 4: with fallback_active=1 in state.db, scanner returns skipped without invoking cli."""
    from sqlalchemy.orm import Session
    from trading_bot.state_fallback import set_flag

    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="strategy_coach", reason="test")

    role = StockScannerRole(engine=engine)
    with patch("trading_bot.cli.intel_scan") as mock_cmd:
        mock_cmd.callback = MagicMock()
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs.get("skipped") is True
    assert result.outputs.get("reason") == "fallback_active"
    assert not mock_cmd.callback.called
