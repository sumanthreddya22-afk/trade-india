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
    role = StrategyCoachRole(engine=engine, bot_start_date=dt.date(2025, 1, 1))
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
    role = StrategyCoachRole(engine=engine, bot_start_date=dt.date(2025, 1, 1))
    with patch.object(role, "_alpha_at", return_value=_alpha(1.8)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert result.outputs["current_state"] == "active"


def test_off_flips_on_when_alpha_below_low(engine):
    role = StrategyCoachRole(engine=engine, bot_start_date=dt.date(2025, 1, 1))
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
    role = StrategyCoachRole(engine=engine, bot_start_date=dt.date(2025, 1, 1))
    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="bootstrap", reason="setup")
    with patch.object(role, "_alpha_at", return_value=_alpha(1.55)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert result.outputs["current_state"] == "fallback"


def test_on_flips_off_with_full_hysteresis(engine):
    role = StrategyCoachRole(engine=engine, bot_start_date=dt.date(2025, 1, 1))
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
    role = StrategyCoachRole(engine=engine, bot_start_date=dt.date(2025, 1, 1))
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


# ---------------------------------------------------------------------------
# Warmup-guard tests (Phase 3.1) — added 2026-05-01 after the misfire.
# ---------------------------------------------------------------------------


def test_warmup_period_blocks_flip_on_with_bad_alpha(engine):
    """Regression: with bot_start = today, the gate must NOT flip even when
    alpha is dismal. Below MIN_DAYS_BEFORE_FALLBACK days the alpha number
    is statistical noise from a tiny sample.
    """
    today = dt.date.today()
    bot_start = today - dt.timedelta(days=4)  # 4 days < 21
    role = StrategyCoachRole(engine=engine, bot_start_date=bot_start)
    with patch.object(role, "_alpha_at", return_value=_alpha(0.0, n=99)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert result.outputs["reason"] == "warmup_period"
    assert result.outputs["days_live"] == 4
    assert result.outputs["min_days_required"] == 21
    with Session(engine) as s:
        assert s.query(FallbackFlag).count() == 0


def test_warmup_does_not_apply_after_threshold(engine):
    """22 days live + bad alpha → gate flips ON (warmup elapsed)."""
    today = dt.date.today()
    bot_start = today - dt.timedelta(days=22)
    role = StrategyCoachRole(engine=engine, bot_start_date=bot_start)
    with patch.object(role, "_alpha_at", return_value=_alpha(0.5, n=99)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is True
    assert result.outputs["new_state"] == "fallback_active"


def test_warmup_resolved_from_fallback_flags_bootstrap_row(engine):
    """Without an explicit bot_start_date, the role reads MIN(set_at)
    from fallback_flags. Bootstrap row is dated today → warmup blocks."""
    role = StrategyCoachRole(engine=engine, bot_start_date=None)
    with Session(engine) as s:
        set_flag(s, fallback_active=False, set_by="bootstrap", reason="initial")
    with patch.object(role, "_alpha_at", return_value=_alpha(0.0, n=99)):
        result = role.safe_run(ctx={})
    assert result.outputs["flag_change"] is False
    assert result.outputs["reason"] == "warmup_period"


def test_warmup_resolved_from_bot_meta_override(engine):
    """bot_meta['bot_start_date'] takes precedence over fallback_flags."""
    from sqlalchemy import text
    today = dt.date.today()
    role = StrategyCoachRole(engine=engine, bot_start_date=None)
    with Session(engine) as s:
        # Old bootstrap row — would otherwise satisfy warmup.
        set_flag(s, fallback_active=False, set_by="bootstrap", reason="initial")
        # bot_meta override says we just started — should take precedence
        # and force warmup behaviour.
        s.execute(text("CREATE TABLE IF NOT EXISTS bot_meta (key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL)"))
        s.execute(text("INSERT INTO bot_meta (key, value) VALUES ('bot_start_date', :v)"),
                  {"v": today.isoformat()})
        s.commit()
    with patch.object(role, "_alpha_at", return_value=_alpha(0.0, n=99)):
        result = role.safe_run(ctx={})
    assert result.outputs["reason"] == "warmup_period"
    assert result.outputs["days_live"] == 0


def test_no_start_date_skips_warmup_gracefully(engine):
    """Empty fallback_flags + no bot_meta override → start_date unknown.
    Role proceeds (logs nothing about warmup) and the alpha flow runs.
    """
    role = StrategyCoachRole(engine=engine, bot_start_date=None)
    with patch.object(role, "_alpha_at", return_value=_alpha(0.5, n=99)):
        result = role.safe_run(ctx={})
    # No warmup reason — falls through to the alpha-based flip.
    assert result.outputs.get("reason") != "warmup_period"
    assert result.outputs["flag_change"] is True
