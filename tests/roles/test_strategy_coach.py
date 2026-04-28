"""StrategyCoachRole state-machine tests."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.roles.strategy_coach import StrategyCoachRole
from trading_bot.state_db import Base, FallbackFlag
from trading_bot.state_fallback import current_flag, set_flag


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _alpha(value: float, *, n: int = 10):
    return {
        "n_trades": n,
        "alpha_multiplier": value,
        "strategy_return_pct": 0.0,
        "spy_return_pct": 0.0,
        "insufficient_data": False,
    }


def test_insufficient_data_no_change(engine):
    role = StrategyCoachRole(engine=engine)
    with patch.object(
        role,
        "_alpha_at",
        return_value={"insufficient_data": True, "n_trades": 2, "alpha_multiplier": 0.0},
    ):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert result.outputs["reason"] == "insufficient_data"
    # No flag rows written
    with Session(engine) as s:
        assert s.query(FallbackFlag).count() == 0


def test_off_stays_off_when_alpha_above_low(engine):
    role = StrategyCoachRole(engine=engine)
    with patch.object(role, "_alpha_at", return_value=_alpha(1.8)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert result.outputs["current_state"] == "active"


def test_off_flips_on_when_alpha_below_low(engine):
    role = StrategyCoachRole(engine=engine)
    with patch.object(role, "_alpha_at", return_value=_alpha(1.2)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is True
    assert result.outputs["new_state"] == "fallback_active"
    with Session(engine) as s:
        flag = current_flag(s)
    assert flag is not None
    assert flag.fallback_active == 1
    assert flag.set_by == "strategy_coach"


def test_on_stays_on_when_alpha_below_high(engine):
    role = StrategyCoachRole(engine=engine)
    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="bootstrap", reason="setup")
    with patch.object(role, "_alpha_at", return_value=_alpha(1.55)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert result.outputs["current_state"] == "fallback"


def test_on_flips_off_with_full_hysteresis(engine):
    role = StrategyCoachRole(engine=engine)
    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="bootstrap", reason="setup")
    # Today: 1.7, days back 1-4: all 1.55+ (above 1.5)
    side_effects = [
        _alpha(1.7),     # today
        _alpha(1.6),     # 1d back
        _alpha(1.55),    # 2d back
        _alpha(1.6),     # 3d back
        _alpha(1.7),     # 4d back
    ]
    with patch.object(role, "_alpha_at", side_effect=side_effects):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is True
    assert result.outputs["new_state"] == "active"
    with Session(engine) as s:
        flag = current_flag(s)
    assert flag.fallback_active == 0


def test_on_does_not_flip_off_if_hysteresis_breaks(engine):
    role = StrategyCoachRole(engine=engine)
    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="bootstrap", reason="setup")
    side_effects = [
        _alpha(1.7),     # today crosses high
        _alpha(1.4),     # 1d back falls under low → sustained=False
    ]
    with patch.object(role, "_alpha_at", side_effect=side_effects):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert "hysteresis" in result.outputs.get("reason", "")
