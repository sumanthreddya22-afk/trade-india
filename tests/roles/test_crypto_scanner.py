# tests/roles/test_crypto_scanner.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.crypto_scanner import CryptoScannerRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = CryptoScannerRole(engine=None)
    assert role.name == "crypto_scanner"
    assert role.tier == 2
    assert "24/7" in role.job_description or "crypto" in role.job_description.lower()


def test_do_work_invokes_crypto_scan(engine):
    role = CryptoScannerRole(engine=engine)
    with patch("trading_bot.cli.crypto_scan") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
        assert mock_cmd.callback.called
    assert result.status == RoleStatus.OK


def test_do_work_handles_exception(engine):
    role = CryptoScannerRole(engine=engine)
    with patch("trading_bot.cli.crypto_scan") as mock_cmd:
        mock_cmd.callback.side_effect = RuntimeError("nope")
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR


def test_kpi_default(engine):
    role = CryptoScannerRole(engine=engine)
    name, value, _ = role._kpi_value(lookback_days=30)
    assert name == "buy_win_rate_5d"
