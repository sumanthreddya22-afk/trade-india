"""lab_data view tests."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.lab_data import (
    active_halts,
    calibrator,
    hold_spy_transition,
    lab_evolution,
    llm_spend,
    recent_proposals,
    role_health,
    strategy_mode,
)
from trading_bot.state_db import (
    AnthropicCostLog,
    Base,
    CalibrationRun,
    CostHalt,
    EvolutionRun,
    HoldSpyTransitionState,
    Leaderboard,
    PromoterHalt,
    RoleRun,
    TemplateProposal,
)
from trading_bot.state_fallback import set_flag


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def test_strategy_mode_none_when_empty(engine):
    with Session(engine) as s:
        assert strategy_mode(s) is None


def test_strategy_mode_active(engine):
    with Session(engine) as s:
        set_flag(s, fallback_active=False, set_by="bootstrap", reason="initial")
        view = strategy_mode(s)
    assert view is not None
    assert view.is_fallback is False
    assert view.label == "ACTIVE"
    assert view.color == "green"


def test_strategy_mode_fallback(engine):
    with Session(engine) as s:
        set_flag(
            s, fallback_active=True, set_by="strategy_coach", reason="alpha drop"
        )
        view = strategy_mode(s)
    assert view.is_fallback is True
    assert view.label == "FALLBACK"
    assert view.color == "amber"


def test_active_halts_empty(engine):
    with Session(engine) as s:
        assert active_halts(s) == []


def test_active_halts_promoter(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(
            PromoterHalt(
                halted_until=now + dt.timedelta(days=3),
                reason="drift",
                set_by="calibrator",
                set_at=now,
            )
        )
        s.commit()
        halts = active_halts(s)
    assert len(halts) == 1
    assert halts[0].kind == "promoter"
    assert 0 < halts[0].hours_remaining < 100


def test_active_halts_excludes_expired(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(
            PromoterHalt(
                halted_until=now - dt.timedelta(hours=1),
                reason="old",
                set_by="calibrator",
                set_at=now - dt.timedelta(days=8),
            )
        )
        s.commit()
        assert active_halts(s) == []


def test_lab_evolution_empty(engine):
    with Session(engine) as s:
        view = lab_evolution(s)
    assert view.last_run_started_at is None
    assert view.top_leaderboard == []


def test_lab_evolution_populated(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(
            EvolutionRun(
                started_at=now,
                finished_at=now,
                template_name="momentum",
                n_trials=50,
                best_fitness=2.5,
                best_params_hash="h",
                auto_promoted=0,
            )
        )
        for fit in (2.5, 2.0, 1.8):
            s.add(
                Leaderboard(
                    template_name="momentum",
                    params_hash=f"h_{fit}",
                    params_json="{}",
                    alpha_vs_spy_x=fit,
                    sortino=1.2,
                    max_dd_pct=15.0,
                    folds_passed=6,
                    folds_total=6,
                    fitness_score=fit,
                    recorded_at=now,
                )
            )
        s.commit()
        view = lab_evolution(s)
    assert view.last_run_n_trials == 50
    assert view.last_run_best_fitness == 2.5
    assert len(view.top_leaderboard) == 3
    assert view.top_leaderboard[0]["fitness_score"] == 2.5


def test_calibrator_view_empty(engine):
    with Session(engine) as s:
        view = calibrator(s)
    assert view.latest_corr is None
    assert view.latest_severity == "never_run"
    assert view.history == []


def test_calibrator_view_populated(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for i, sev in enumerate(["ok", "warning", "ok"]):
            s.add(
                CalibrationRun(
                    recorded_at=now - dt.timedelta(days=i),
                    template_name="momentum",
                    n_trades=20,
                    spearman_corr=0.4,
                    severity=sev,
                )
            )
        s.commit()
        view = calibrator(s, history_days=7)
    assert view.latest_severity == "ok"
    assert len(view.history) == 3


def test_llm_spend_empty(engine):
    with Session(engine) as s:
        view = llm_spend(s)
    assert view.month_to_date_usd == 0.0
    assert view.n_calls_mtd == 0


def test_llm_spend_records_calls(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(
            AnthropicCostLog(
                called_at=now,
                role_name="strategy_architect",
                model="claude-opus-4-7",
                input_tokens=1000,
                output_tokens=2000,
                cost_usd=0.165,
            )
        )
        s.commit()
        view = llm_spend(s)
    assert view.n_calls_mtd == 1
    assert view.month_to_date_usd > 0


def test_role_health_empty(engine):
    with Session(engine) as s:
        assert role_health(s) == []


def test_role_health_aggregates(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for i in range(10):
            status = "ok" if i < 8 else "error"
            s.add(
                RoleRun(
                    role_name="stock_scanner",
                    started_at=now - dt.timedelta(hours=i),
                    finished_at=now - dt.timedelta(hours=i),
                    status=status,
                    latency_ms=100,
                )
            )
        s.commit()
        rows = role_health(s)
    assert len(rows) == 1
    assert rows[0].role_name == "stock_scanner"
    assert rows[0].runs_30d == 10
    assert rows[0].success_rate_pct == 80.0


def test_recent_proposals_empty(engine):
    with Session(engine) as s:
        assert recent_proposals(s) == []


def test_recent_proposals_ordered(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for i, name in enumerate(["v1", "v2", "v3"]):
            s.add(
                TemplateProposal(
                    proposed_at=now - dt.timedelta(days=i),
                    name=name,
                    rationale="test rationale",
                    expected_regime="trending_up",
                    code="pass",
                    tests="pass",
                    params_to_search_json="{}",
                    review_status="pending",
                )
            )
        s.commit()
        rows = recent_proposals(s, limit=2)
    assert len(rows) == 2
    assert rows[0].name == "v1"  # most recent first
