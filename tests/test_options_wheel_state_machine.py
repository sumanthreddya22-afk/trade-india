"""Tests for the options wheel-state machine (Phase 3 scaffold + 3B).

Covers:
  - open_cycle creates a CSP_OPEN row + audit history
  - allowed transitions advance state + accumulate premium
  - disallowed transitions raise InvalidTransition without writing
  - close_cycle requires legal terminal precondition
  - history returns transition rows in chronological order
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.state_db import (
    WheelCycleOptions,
    WheelStateHistoryOptions,
)
from trading_bot.pipelines.options.wheel_state import (
    CycleSnapshot,
    InvalidTransition,
    WheelState,
    advance,
    close_cycle,
    history,
    open_cycle,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def test_open_cycle_starts_in_csp_open(engine):
    cycle_id = open_cycle(
        engine, underlying="AAPL",
        initial_csp_strike=180.0,
        target_delta_csp=0.20, target_delta_cc=0.30,
    )
    assert cycle_id > 0
    with Session(engine) as session:
        cycle = session.get(WheelCycleOptions, cycle_id)
    assert cycle is not None
    assert cycle.state == "csp_open"
    assert cycle.initial_csp_strike == 180.0


def test_open_cycle_writes_initial_history_row(engine):
    cycle_id = open_cycle(
        engine, underlying="AAPL",
        initial_csp_strike=180.0,
        target_delta_csp=0.20, target_delta_cc=0.30,
    )
    rows = history(engine, cycle_id=cycle_id)
    assert len(rows) == 1
    assert rows[0].from_state == "cash"
    assert rows[0].to_state == "csp_open"


def test_csp_to_assigned_to_cc_open_to_called_away(engine):
    """Walk the full happy-path lifecycle."""
    cycle_id = open_cycle(
        engine, underlying="AAPL",
        initial_csp_strike=180.0,
        target_delta_csp=0.20, target_delta_cc=0.30,
    )
    snap = advance(
        engine, cycle_id=cycle_id, to_state=WheelState.ASSIGNED,
        transition="csp_assigned",
        assignment_share_basis=180.0,
        premium_delta=2.50,
    )
    assert snap.state is WheelState.ASSIGNED
    assert snap.assignment_share_basis == 180.0
    assert snap.cumulative_premium == 2.50

    snap = advance(
        engine, cycle_id=cycle_id, to_state=WheelState.CC_OPEN,
        transition="cc_opened",
        premium_delta=1.75,
    )
    assert snap.state is WheelState.CC_OPEN
    assert snap.cumulative_premium == pytest.approx(2.50 + 1.75)

    snap = advance(
        engine, cycle_id=cycle_id, to_state=WheelState.CALLED_AWAY,
        transition="cc_called_away",
        final_called_away_at=185.0,
    )
    assert snap.state is WheelState.CALLED_AWAY
    assert snap.final_called_away_at == 185.0

    final = close_cycle(engine, cycle_id=cycle_id, realized_pnl=8.25)
    assert final.state is WheelState.CLOSED
    assert final.realized_pnl == 8.25
    assert final.ended_at is not None

    rows = history(engine, cycle_id=cycle_id)
    transitions = [(r.from_state, r.to_state) for r in rows]
    assert transitions == [
        ("cash", "csp_open"),
        ("csp_open", "assigned"),
        ("assigned", "cc_open"),
        ("cc_open", "called_away"),
        ("called_away", "closed"),
    ]


def test_csp_roll_stays_in_csp_open(engine):
    """A roll should preserve csp_open state and accumulate premium."""
    cycle_id = open_cycle(
        engine, underlying="AAPL",
        initial_csp_strike=180.0,
        target_delta_csp=0.20, target_delta_cc=0.30,
    )
    advance(
        engine, cycle_id=cycle_id, to_state=WheelState.CSP_OPEN,
        transition="csp_rolled_for_credit",
        premium_delta=0.85,
    )
    with Session(engine) as session:
        cycle = session.get(WheelCycleOptions, cycle_id)
    assert cycle.state == "csp_open"
    assert cycle.cumulative_premium == 0.85


def test_disallowed_transition_raises_and_writes_nothing(engine):
    """csp_open → cc_open is illegal (must go through assigned)."""
    cycle_id = open_cycle(
        engine, underlying="AAPL",
        initial_csp_strike=180.0,
        target_delta_csp=0.20, target_delta_cc=0.30,
    )
    with pytest.raises(InvalidTransition):
        advance(
            engine, cycle_id=cycle_id, to_state=WheelState.CC_OPEN,
            transition="illegal",
        )
    # State unchanged + no extra history row
    with Session(engine) as session:
        cycle = session.get(WheelCycleOptions, cycle_id)
    assert cycle.state == "csp_open"
    assert len(history(engine, cycle_id=cycle_id)) == 1  # only the open row


def test_close_cycle_rejects_invalid_precondition(engine):
    cycle_id = open_cycle(
        engine, underlying="AAPL",
        initial_csp_strike=180.0,
        target_delta_csp=0.20, target_delta_cc=0.30,
    )
    with pytest.raises(InvalidTransition):
        close_cycle(engine, cycle_id=cycle_id, realized_pnl=0.0)


def test_csp_expired_path_to_closed(engine):
    """Alternative happy path: CSP expires worthless, cycle closes immediately."""
    cycle_id = open_cycle(
        engine, underlying="AAPL",
        initial_csp_strike=180.0,
        target_delta_csp=0.20, target_delta_cc=0.30,
    )
    advance(
        engine, cycle_id=cycle_id, to_state=WheelState.CSP_EXPIRED,
        transition="csp_expired_worthless",
        premium_delta=2.50,
    )
    snap = close_cycle(engine, cycle_id=cycle_id, realized_pnl=2.50)
    assert snap.state is WheelState.CLOSED


def test_advance_unknown_cycle_raises(engine):
    with pytest.raises(ValueError):
        advance(
            engine, cycle_id=9999, to_state=WheelState.CSP_OPEN,
            transition="phantom",
        )
