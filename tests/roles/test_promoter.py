"""PromoterRole tests."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.roles.base import RoleStatus
from trading_bot.roles.promoter import PromoterRole
from trading_bot.state_db import Base, Leaderboard


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def active_path(tmp_path):
    p = tmp_path / "paper_active.json"
    p.write_text(
        json.dumps(
            {
                "version": "test-v1",
                "active_template": "momentum",
                "params": {"rsi_lower": 55.0, "rsi_upper": 70.0},
                "fitness_at_promotion": 1.0,
                "risk_caps": {"max_position_pct": 10},
            }
        )
    )
    return p


def _add_leaderboard_row(
    session, *, fitness, alpha=1.7, sortino=1.3, dd=15.0, params=None
):
    params = params or {"rsi_lower": 58.0}
    session.add(
        Leaderboard(
            template_name="momentum",
            params_hash="abc",
            params_json=json.dumps(params),
            alpha_vs_spy_x=alpha,
            sortino=sortino,
            max_dd_pct=dd,
            folds_passed=6,
            folds_total=6,
            fitness_score=fitness,
            recorded_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.commit()


def test_promoter_promotes_when_top_clears_gate(engine, active_path):
    role = PromoterRole(engine=engine, active_path=active_path)
    with Session(engine) as s:
        _add_leaderboard_row(s, fitness=1.5, alpha=1.7, sortino=1.3, dd=15.0)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["promoted"] is True
    written = json.loads(active_path.read_text())
    assert written["params"]["rsi_lower"] == 58.0
    assert written["fitness_at_promotion"] == 1.5
    # Unrelated keys preserved
    assert written["risk_caps"] == {"max_position_pct": 10}


def test_promoter_skips_when_top_below_delta_gate(engine, active_path):
    role = PromoterRole(engine=engine, active_path=active_path)
    with Session(engine) as s:
        # Only 5% above current 1.0 — below 10% delta gate
        _add_leaderboard_row(s, fitness=1.05, alpha=1.7, sortino=1.3, dd=15.0)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["promoted"] is False
    written = json.loads(active_path.read_text())
    assert written["params"]["rsi_lower"] == 55.0  # unchanged


def test_promoter_skips_when_top_fails_promotion_gate(engine, active_path):
    role = PromoterRole(engine=engine, active_path=active_path)
    with Session(engine) as s:
        # Massive fitness but alpha below MIN_ALPHA_VS_SPY (1.5)
        _add_leaderboard_row(s, fitness=99.0, alpha=1.4, sortino=1.3, dd=15.0)
    result = role.safe_run(ctx={})
    assert result.outputs["promoted"] is False


def test_promoter_handles_empty_leaderboard(engine, active_path):
    role = PromoterRole(engine=engine, active_path=active_path)
    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["promoted"] is False
    assert "no_candidate" in result.outputs.get("reason", "")
