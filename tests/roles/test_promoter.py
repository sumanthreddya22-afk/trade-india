"""PromoterRole tests."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.roles.base import RoleStatus
from trading_bot.roles.promoter import PromoterRole
from trading_bot.state_db import Base, Leaderboard, PromoterHalt


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


def test_promoter_respects_calibrator_halt(engine, active_path):
    """Even with a winning leaderboard row, an active PromoterHalt blocks promotion."""
    role = PromoterRole(engine=engine, active_path=active_path)
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        # Plant a leaderboard winner that would otherwise promote
        _add_leaderboard_row(s, fitness=1.5, alpha=1.7, sortino=1.3, dd=15.0)
        # And a calibrator halt active for the next 7 days
        s.add(
            PromoterHalt(
                halted_until=now + dt.timedelta(days=7),
                reason="calibrator drift: spearman_corr=-0.5",
                set_by="calibrator",
                set_at=now,
            )
        )
        s.commit()

    result = role.safe_run(ctx={})
    assert result.status == RoleStatus.OK
    assert result.outputs["promoted"] is False
    assert result.outputs["reason"] == "halted_by_calibrator"
    # Active config not touched
    written = json.loads(active_path.read_text())
    assert written["params"]["rsi_lower"] == 55.0


def test_promoter_ignores_expired_halt(engine, active_path):
    """An expired halt should not block promotion."""
    role = PromoterRole(engine=engine, active_path=active_path)
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        _add_leaderboard_row(s, fitness=1.5, alpha=1.7, sortino=1.3, dd=15.0)
        # Halt that ended 1 day ago
        s.add(
            PromoterHalt(
                halted_until=now - dt.timedelta(days=1),
                reason="old halt",
                set_by="calibrator",
                set_at=now - dt.timedelta(days=8),
            )
        )
        s.commit()
    result = role.safe_run(ctx={})
    assert result.outputs["promoted"] is True


def test_promoter_blocked_by_debate(engine, active_path):
    """When the bull/bear debate judge returns a 'block' verdict with
    medium+ confidence, promotion is rejected even though both fitness
    and delta gates passed."""
    from trading_bot.promotion_debate import DebateVerdict
    from unittest.mock import patch

    role = PromoterRole(engine=engine, active_path=active_path)
    with Session(engine) as s:
        _add_leaderboard_row(s, fitness=1.5, alpha=1.7, sortino=1.3, dd=15.0)

    block_verdict = DebateVerdict(
        recommendation="block", confidence="high",
        reason="Bear identified clear overfitting: max_dd shape mismatches lessons",
        bull_text="(stub)", bear_text="(stub)",
    )
    with patch(
        "trading_bot.roles.promoter.run_promotion_debate",
        return_value=block_verdict,
    ):
        result = role.safe_run(ctx={})
    assert result.outputs["promoted"] is False
    assert result.outputs["reason"] == "blocked_by_debate"
    assert result.outputs["debate"]["recommendation"] == "block"
    # Active config NOT touched
    written = json.loads(active_path.read_text())
    assert written["params"]["rsi_lower"] == 55.0


def test_promoter_proceeds_when_debate_inconclusive(engine, active_path):
    """If the debate returns None (LLM unavailable / SDK error / no creds),
    PromoterRole must fall back to the prior behaviour and promote."""
    from unittest.mock import patch

    role = PromoterRole(engine=engine, active_path=active_path)
    with Session(engine) as s:
        _add_leaderboard_row(s, fitness=1.5, alpha=1.7, sortino=1.3, dd=15.0)
    with patch("trading_bot.roles.promoter.run_promotion_debate", return_value=None):
        result = role.safe_run(ctx={})
    assert result.outputs["promoted"] is True
    assert "debate" not in result.outputs


def test_promoter_proceeds_when_debate_promotes(engine, active_path):
    """A 'promote' verdict at any confidence level allows promotion and
    the verdict is attached to outputs for the audit trail."""
    from trading_bot.promotion_debate import DebateVerdict
    from unittest.mock import patch

    role = PromoterRole(engine=engine, active_path=active_path)
    with Session(engine) as s:
        _add_leaderboard_row(s, fitness=1.5, alpha=1.7, sortino=1.3, dd=15.0)
    promote_verdict = DebateVerdict(
        recommendation="promote", confidence="medium",
        reason="Bear concerns are speculative; fold metrics are consistent.",
        bull_text="(stub)", bear_text="(stub)",
    )
    with patch(
        "trading_bot.roles.promoter.run_promotion_debate",
        return_value=promote_verdict,
    ):
        result = role.safe_run(ctx={})
    assert result.outputs["promoted"] is True
    assert result.outputs["debate"]["recommendation"] == "promote"


def test_promoter_block_at_low_confidence_does_not_block(engine, active_path):
    """A 'block' verdict at LOW confidence should not stop promotion —
    we only honour the bear case when the judge is confident."""
    from trading_bot.promotion_debate import DebateVerdict
    from unittest.mock import patch

    role = PromoterRole(engine=engine, active_path=active_path)
    with Session(engine) as s:
        _add_leaderboard_row(s, fitness=1.5, alpha=1.7, sortino=1.3, dd=15.0)
    low_conf_block = DebateVerdict(
        recommendation="block", confidence="low",
        reason="Some uncertainty about the regime fit.",
        bull_text="(stub)", bear_text="(stub)",
    )
    with patch(
        "trading_bot.roles.promoter.run_promotion_debate",
        return_value=low_conf_block,
    ):
        result = role.safe_run(ctx={})
    assert result.outputs["promoted"] is True
