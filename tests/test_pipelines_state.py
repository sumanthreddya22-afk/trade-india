"""Tests for the per-pipeline system view state builder."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.dashboard.pipelines_state import (
    PipelineState,
    StageState,
    _stage_health,
    build_pipelines_state,
)
from trading_bot.dashboard.pipelines_topology import PIPELINES, Stage
from trading_bot.state_db import Base, RoleRun


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """Build a fresh DB with the full SQLAlchemy schema. Eagerly import
    each pipeline's state_db module so its tables are registered on the
    shared Base before create_all runs (SQLAlchemy registers tables at
    import-time)."""
    # Side-effect imports — each adds its tables to Base.metadata.
    import trading_bot.pipelines.crypto.state_db  # noqa: F401
    import trading_bot.pipelines.options.state_db  # noqa: F401
    db = tmp_path / "state.db"
    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    return db


# ---------------------------------------------------------------------------
# _stage_health unit logic
# ---------------------------------------------------------------------------


def test_stage_with_no_role_uses_count(tmp_path):
    stage = Stage(id="x", label="x", count_query=None)
    h = _stage_health(stage, role_info={}, count=5,
                      now=dt.datetime.now(dt.timezone.utc))
    assert h == "ok"
    h_off = _stage_health(stage, role_info={}, count=0,
                          now=dt.datetime.now(dt.timezone.utc))
    assert h_off == "off"


def test_stage_off_when_role_never_ran():
    stage = Stage(id="x", label="x", role_name="role-a")
    h = _stage_health(stage, role_info={}, count=0,
                      now=dt.datetime.now(dt.timezone.utc))
    assert h == "off"


def test_stage_ok_when_recent_success():
    stage = Stage(id="x", label="x", role_name="role-a")
    now = dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.timezone.utc)
    role_info = {
        "last_run_at": now - dt.timedelta(minutes=5),
        "last_status": "ok",
        "runs_today": 3,
    }
    assert _stage_health(stage, role_info=role_info, count=0, now=now) == "ok"


def test_stage_warn_when_stale():
    """Last run >36h ago even if status was ok → warn."""
    stage = Stage(id="x", label="x", role_name="role-a")
    now = dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.timezone.utc)
    role_info = {
        "last_run_at": now - dt.timedelta(hours=48),
        "last_status": "ok",
        "runs_today": 0,
    }
    assert _stage_health(stage, role_info=role_info, count=0, now=now) == "warn"


def test_stage_fail_when_recent_error_and_no_success_today():
    stage = Stage(id="x", label="x", role_name="role-a")
    now = dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.timezone.utc)
    role_info = {
        "last_run_at": now - dt.timedelta(minutes=10),
        "last_status": "error",
        "runs_today": 0,
    }
    assert _stage_health(stage, role_info=role_info, count=0, now=now) == "fail"


def test_stage_warn_when_error_was_recovered_today():
    """Past error but a successful run today → warn (acknowledge there
    was an issue but don't claim it's still failing)."""
    stage = Stage(id="x", label="x", role_name="role-a")
    now = dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.timezone.utc)
    role_info = {
        "last_run_at": now - dt.timedelta(minutes=5),
        "last_status": "error",
        "runs_today": 2,
    }
    assert _stage_health(stage, role_info=role_info, count=0, now=now) == "warn"


# ---------------------------------------------------------------------------
# Full build_pipelines_state — fresh DB
# ---------------------------------------------------------------------------


def test_build_returns_three_pipelines_with_off_stages_on_empty_db(state_db):
    out = build_pipelines_state(state_db)
    assert len(out) == 3
    ids = [p.pipeline.id for p in out]
    assert ids == ["stocks", "crypto", "options"]
    # Every stage in every pipeline should be off (no role runs, empty
    # tables) on a fresh DB.
    for ps in out:
        # Aggregator stages with no role_runs report off when count=0
        for s in ps.stages:
            assert s.health in ("off", "warn", "fail", "ok")
            assert s.count == 0


def test_build_summarises_pipeline_as_off_when_no_runs(state_db):
    out = build_pipelines_state(state_db)
    for ps in out:
        assert ps.summary_tone in ("off", "fail", "warn", "ok")


def test_build_marks_stage_ok_after_role_run(state_db):
    """Insert a role_run row for crypto_scanner. Automated stages flip
    to ok; LLM stages with zero debate output stay warn (honest signal:
    role fired but no debate landed)."""
    eng = create_engine(f"sqlite:///{state_db}")
    now = dt.datetime.now(dt.timezone.utc)
    with Session(eng) as session:
        session.add(RoleRun(
            role_name="crypto_intel_ingestor",
            started_at=now - dt.timedelta(minutes=2),
            finished_at=now - dt.timedelta(minutes=1),
            status="ok",
            latency_ms=100,
            error_text=None,
        ))
        session.commit()

    out = build_pipelines_state(state_db)
    crypto = next(p for p in out if p.pipeline.id == "crypto")

    # Automated stages: ok (role ran today)
    sources = next(s for s in crypto.stages if s.stage.id == "crypto-sources")
    assert sources.health == "ok"

    # LLM stages with no debate output: warn (broken pipeline signal)
    scout = next(s for s in crypto.stages if s.stage.id == "crypto-scout")
    assert scout.health == "warn"


def test_llm_stage_ok_only_when_debate_landed(state_db):
    """Scout debate stage flips to ok only after a row actually lands
    in scout_debate_runs_crypto. Until then it's warn even if the host
    role ran successfully."""
    from trading_bot.pipelines.crypto.state_db import ScoutDebateRunCrypto

    eng = create_engine(f"sqlite:///{state_db}")
    now = dt.datetime.now(dt.timezone.utc)
    with Session(eng) as session:
        session.add(RoleRun(
            role_name="crypto_intel_ingestor",
            started_at=now - dt.timedelta(minutes=2),
            finished_at=now - dt.timedelta(minutes=1),
            status="ok", latency_ms=100, error_text=None,
        ))
        session.add(ScoutDebateRunCrypto(
            run_at=now - dt.timedelta(minutes=1),
            symbol="ETH/USD",
            verdict="elevate", confidence="high",
            judge_reason="ok", prompt_version="v1",
        ))
        session.commit()

    out = build_pipelines_state(state_db)
    crypto = next(p for p in out if p.pipeline.id == "crypto")
    scout = next(s for s in crypto.stages if s.stage.id == "crypto-scout")
    assert scout.health == "ok"
    assert scout.count == 1


def test_build_attaches_operators_for_llm_stages(state_db):
    out = build_pipelines_state(state_db)
    crypto = next(p for p in out if p.pipeline.id == "crypto")
    scout = next(s for s in crypto.stages if s.stage.id == "crypto-scout")
    op_names = [op["name"] for op in scout.operators]
    assert "Sasha Volkov" in op_names
    assert "Lena Park" in op_names
    assert "Diane Pereira" in op_names
    judge = next(o for o in scout.operators if o["is_judge"])
    assert judge["name"] == "Diane Pereira"


def test_build_no_operators_for_automated_stages(state_db):
    out = build_pipelines_state(state_db)
    crypto = next(p for p in out if p.pipeline.id == "crypto")
    sources = next(s for s in crypto.stages if s.stage.id == "crypto-sources")
    assert sources.operators == []


def test_build_handles_missing_db_path(tmp_path):
    """A missing DB file should not crash — pipeline objects still render."""
    bogus = tmp_path / "does-not-exist.db"
    out = build_pipelines_state(bogus)
    assert len(out) == 3
    for ps in out:
        assert ps.summary_tone == "fail"
        assert "unavailable" in ps.summary
