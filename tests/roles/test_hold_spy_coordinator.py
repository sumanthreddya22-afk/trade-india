"""HoldSpyCoordinatorRole tests."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.roles.hold_spy_coordinator import HoldSpyCoordinatorRole
from trading_bot.state_db import Base, FallbackFlag, HoldSpyTransitionState
from trading_bot.state_fallback import set_flag


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _fake_position(symbol: str, qty: float, market_value: float, asset_class: str = "us_equity"):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.market_value = Decimal(str(market_value))
    p.asset_class = asset_class
    return p


def _fake_client():
    client = MagicMock()
    client.place_market_order = MagicMock(return_value="orderid-fake")
    return client


def test_skipped_when_no_flag(engine):
    role = HoldSpyCoordinatorRole(engine=engine, alpaca_client=_fake_client())
    result = role.safe_run(ctx={})
    assert result.outputs.get("skipped") is True
    assert result.outputs["reason"] == "no_fallback_flag"


def test_exit_phase_sells_one_fifth(engine):
    """Day 1 of fallback: sells 1/5 of each active position, buys SPY."""
    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="strategy_coach", reason="alpha drop")
    client = _fake_client()
    client.list_positions = MagicMock(
        return_value=[
            _fake_position("AAPL", 50, 7500),  # 50 shares at ~$150
            _fake_position("TSLA", 25, 5000),  # 25 shares at $200
        ]
    )
    role = HoldSpyCoordinatorRole(engine=engine, alpaca_client=client)
    result = role.safe_run(ctx={})

    actions = result.outputs["actions"]
    sells = [a for a in actions if a["action"] == "sell"]
    # Day 0, remaining = 5 days. 50/5 = 10 AAPL, 25/5 = 5 TSLA
    aapl = next((a for a in sells if a["symbol"] == "AAPL"), None)
    tsla = next((a for a in sells if a["symbol"] == "TSLA"), None)
    assert aapl is not None and aapl["qty"] == 10.0
    assert tsla is not None and tsla["qty"] == 5.0
    # Should have a buy_spy action too (freed cash 10*$150 + 5*$200 = $2500)
    buys = [a for a in actions if a["action"] == "buy_spy"]
    assert len(buys) == 1


def test_exit_phase_idempotent_within_day(engine):
    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="strategy_coach", reason="x")
    client = _fake_client()
    client.list_positions = MagicMock(return_value=[_fake_position("AAPL", 50, 7500)])
    role = HoldSpyCoordinatorRole(engine=engine, alpaca_client=client)
    result1 = role.safe_run(ctx={})
    result2 = role.safe_run(ctx={})
    assert result1.outputs["day_index_advanced_to"] == 1
    assert result2.outputs.get("skipped") is True
    assert result2.outputs["reason"] == "already_acted_today"


def test_reverse_phase_sells_spy(engine):
    """When fallback flips back OFF, sells 1/5 SPY each day."""
    with Session(engine) as s:
        set_flag(s, fallback_active=False, set_by="strategy_coach", reason="resume")
    client = _fake_client()
    client.list_positions = MagicMock(
        return_value=[_fake_position("SPY", 50, 25000)]  # 50 shares at $500
    )
    role = HoldSpyCoordinatorRole(engine=engine, alpaca_client=client)
    result = role.safe_run(ctx={})
    actions = result.outputs["actions"]
    spy_sells = [a for a in actions if a["action"] == "sell_spy"]
    assert len(spy_sells) == 1
    assert spy_sells[0]["qty"] == 10.0  # 50 / 5


def test_transition_complete_after_5_days(engine):
    with Session(engine) as s:
        flag = set_flag(s, fallback_active=True, set_by="strategy_coach", reason="x")
        # Pre-seed transition state at day 5 (already done)
        s.add(
            HoldSpyTransitionState(
                fallback_flag_id=flag.id,
                phase="exit",
                day_index=5,
                last_action_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10),
            )
        )
        s.commit()
    client = _fake_client()
    client.list_positions = MagicMock(return_value=[])
    role = HoldSpyCoordinatorRole(engine=engine, alpaca_client=client)
    result = role.safe_run(ctx={})
    assert result.outputs.get("skipped") is True
    assert result.outputs["reason"] == "transition_complete"
