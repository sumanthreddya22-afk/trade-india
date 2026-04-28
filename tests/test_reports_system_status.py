"""build_system_status_section integration tests."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.reports import build_system_status_section
from trading_bot.state_db import (
    AnthropicCostLog,
    Base,
    CalibrationRun,
    EvolutionRun,
    Leaderboard,
    PromoterHalt,
    RoleRun,
)
from trading_bot.state_fallback import set_flag


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def test_renders_with_empty_state_db(engine):
    """Even with zero rows, the block renders without raising."""
    html = build_system_status_section(engine)
    assert "Strategy Mode" in html
    assert "Lab Evolution" in html
    assert "Calibrator" in html
    assert "LLM Spend" in html
    assert "Role Health" in html


def test_strategy_mode_active_renders_green(engine):
    with Session(engine) as s:
        set_flag(s, fallback_active=False, set_by="bootstrap", reason="initial")
    html = build_system_status_section(engine)
    assert "ACTIVE" in html


def test_strategy_mode_fallback_renders_amber(engine):
    with Session(engine) as s:
        set_flag(s, fallback_active=True, set_by="strategy_coach", reason="alpha drop")
    html = build_system_status_section(engine)
    assert "FALLBACK" in html


def test_active_halt_appears_in_block(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(
            PromoterHalt(
                halted_until=now + dt.timedelta(days=3),
                reason="calibrator drift",
                set_by="calibrator",
                set_at=now,
            )
        )
        s.commit()
    html = build_system_status_section(engine)
    assert "Active Halts" in html
    assert "promoter" in html
    assert "drift" in html


def test_no_halts_section_when_none_active(engine):
    html = build_system_status_section(engine)
    assert "Active Halts" not in html


def test_lab_evolution_renders_with_leaderboard(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(
            EvolutionRun(
                started_at=now,
                finished_at=now,
                template_name="momentum",
                n_trials=42,
                best_fitness=2.4,
                best_params_hash="h",
                auto_promoted=0,
            )
        )
        s.add(
            Leaderboard(
                template_name="momentum", params_hash="h",
                params_json="{}", alpha_vs_spy_x=2.4, sortino=1.5,
                max_dd_pct=12.0, folds_passed=6, folds_total=6,
                fitness_score=2.4, recorded_at=now,
            )
        )
        s.commit()
    html = build_system_status_section(engine)
    assert "42 trials" in html
    assert "momentum" in html


def test_llm_spend_block(engine):
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
    html = build_system_status_section(engine)
    assert "$0.17" in html or "$0.16" in html  # rounding can go either way
    assert "claude-opus-4-7" in html


def test_role_health_block(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for i in range(5):
            s.add(
                RoleRun(
                    role_name="health_pulse",
                    started_at=now - dt.timedelta(minutes=i),
                    finished_at=now - dt.timedelta(minutes=i),
                    status="ok",
                    latency_ms=5,
                )
            )
        s.commit()
    html = build_system_status_section(engine)
    assert "health_pulse" in html
    assert "100%" in html


def test_calibrator_block_with_high_severity(engine):
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(
            CalibrationRun(
                recorded_at=now,
                template_name="momentum",
                n_trades=15,
                spearman_corr=0.15,
                severity="high",
            )
        )
        s.commit()
    html = build_system_status_section(engine)
    assert "0.15" in html
    assert "high" in html
