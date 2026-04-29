# tests/test_wheel_state.py
import datetime as dt
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.options.wheel_state import (
    WheelStateRepo, Phase, open_csp, mark_assigned, open_cc, close_cycle,
)
from trading_bot.state_db import Base, WheelCycle


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'w.db'}")
    Base.metadata.create_all(e)
    return e


def test_open_csp_creates_cycle(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc is not None
    assert cyc.cycle_id == cid and cyc.phase == Phase.CSP_OPEN.value
    assert cyc.csp_strike == Decimal("190")


def test_mark_assigned_advances_phase_and_records_cost_basis(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    mark_assigned(repo, cycle_id=cid, when=dt.datetime.now(dt.timezone.utc))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc.phase == Phase.ASSIGNED.value
    # cost basis = strike − credit
    assert cyc.cost_basis == Decimal("187.90")


def test_open_cc_after_assignment(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    mark_assigned(repo, cycle_id=cid, when=dt.datetime.now(dt.timezone.utc))
    open_cc(repo, cycle_id=cid, contract="AAPL250620C00195000",
            strike=Decimal("195"), expiration=dt.date(2025, 6, 20),
            credit=Decimal("1.10"))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc.phase == Phase.CC_OPEN.value
    assert cyc.cc_strike == Decimal("195")


def test_close_cycle_finalizes(engine):
    repo = WheelStateRepo(engine)
    cid = open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
                   strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
                   credit=Decimal("2.10"))
    close_cycle(repo, cycle_id=cid, realized_pnl=Decimal("105"))
    cyc = repo.get_active(symbol="AAPL")
    assert cyc is None  # no active cycle


def test_no_two_active_cycles_for_same_symbol(engine):
    repo = WheelStateRepo(engine)
    open_csp(repo, symbol="AAPL", contract="AAPL250516P00190000",
             strike=Decimal("190"), expiration=dt.date(2025, 5, 16),
             credit=Decimal("2.10"))
    with pytest.raises(ValueError, match="active cycle exists"):
        open_csp(repo, symbol="AAPL", contract="AAPL250620P00185000",
                 strike=Decimal("185"), expiration=dt.date(2025, 6, 20),
                 credit=Decimal("1.80"))
