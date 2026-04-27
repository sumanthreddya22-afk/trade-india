import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base
from trading_bot.state_hwm import update_hwm
from trading_bot.watchdog_account import AccountSentinel, ReconcileVerdict


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    yield engine
    os.unlink(path)


@pytest.fixture
def alpaca():
    """Fake Alpaca client returning a stubbed account."""
    a = MagicMock()
    return a


def test_no_drawdown_when_equity_at_hwm(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("100000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    v = s.check()
    assert v.drawdown_pct == 0.0
    assert v.paused is False
    assert not (tmp_path / "pause.flag").exists()


def test_pauses_on_drawdown_breach(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("78000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    v = s.check()
    assert v.drawdown_pct > 20.0
    assert v.paused is True
    assert (tmp_path / "pause.flag").exists()


def test_does_not_pause_below_threshold(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("82000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    v = s.check()
    assert 17.0 < v.drawdown_pct < 20.0
    assert v.paused is False


def test_advances_hwm_on_new_high(db, alpaca, tmp_path):
    alpaca.get_account.return_value = MagicMock(equity=Decimal("105000"))
    s = AccountSentinel(
        engine=db,
        alpaca=alpaca,
        pause_flag_path=tmp_path / "pause.flag",
        max_dd_pct=20.0,
        account="paper",
    )
    with Session(db) as sess:
        update_hwm(sess, account="paper", equity=100_000.0)
    s.check()
    with Session(db) as sess:
        from trading_bot.state_hwm import current_hwm
        assert current_hwm(sess, account="paper") == pytest.approx(105_000.0)
