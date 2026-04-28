import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base, EquityHighWaterMark
from trading_bot.state_hwm import update_hwm, current_hwm, drawdown_pct


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    yield engine
    os.unlink(path)


def test_current_hwm_none_when_empty(db):
    with Session(db) as s:
        assert current_hwm(s, account="paper") is None


def test_update_hwm_writes_and_returns(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
        assert current_hwm(s, account="paper") == pytest.approx(100_000.0)


def test_update_hwm_only_advances(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
        update_hwm(s, account="paper", equity=99_000.0)  # below — should not advance
        assert current_hwm(s, account="paper") == pytest.approx(100_000.0)
        update_hwm(s, account="paper", equity=101_000.0)
        assert current_hwm(s, account="paper") == pytest.approx(101_000.0)


def test_drawdown_pct(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
    with Session(db) as s:
        assert drawdown_pct(s, account="paper", current_equity=80_000.0) == pytest.approx(20.0)
        assert drawdown_pct(s, account="paper", current_equity=100_000.0) == pytest.approx(0.0)
        assert drawdown_pct(s, account="paper", current_equity=110_000.0) == pytest.approx(0.0)


def test_drawdown_pct_no_hwm_returns_zero(db):
    with Session(db) as s:
        # No HWM written yet — no drawdown can be computed
        assert drawdown_pct(s, account="paper", current_equity=80_000.0) == 0.0


def test_accounts_isolated(db):
    with Session(db) as s:
        update_hwm(s, account="paper", equity=100_000.0)
        update_hwm(s, account="live", equity=5_000.0)
    with Session(db) as s:
        assert current_hwm(s, account="paper") == pytest.approx(100_000.0)
        assert current_hwm(s, account="live") == pytest.approx(5_000.0)
