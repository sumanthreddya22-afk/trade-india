"""Tests for supervisor.{_build_role_sla_map, _find_stalled_roles}.

The supervisor's generic role stall watchdog (Phase 7.2) reads role_runs
and flags any role whose started_at is older than 2× sla_seconds with
finished_at still NULL. These tests verify the predicate against
synthetic role_runs rows.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def _setup_state_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    from trading_bot.state_db import Base
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(eng)
    return db_path


def _insert_run(db_path: Path, *, role_name: str,
                started_seconds_ago: int,
                finished: bool = False,
                status: str = "ok"):
    from trading_bot.state_db import RoleRun
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    started = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(seconds=started_seconds_ago))
    finished_at = (started + dt.timedelta(seconds=1)) if finished else None
    with Session(eng) as s:
        s.add(RoleRun(
            role_name=role_name, started_at=started,
            finished_at=finished_at, status=status,
        ))
        s.commit()


def test_role_sla_map_has_known_roles():
    """Sanity check: the supervisor sees the standard role list."""
    from trading_bot.supervisor import _build_role_sla_map
    m = _build_role_sla_map()
    # A handful of stable, known roles should be present.
    for expected in ["strategy_coach", "stock_scanner", "crypto_scanner",
                     "watchdog", "decision_reflector", "param_optimizer"]:
        assert expected in m, f"missing role {expected} from SLA map"
    # All values are sane positive integers.
    for k, v in m.items():
        assert v > 0, f"{k} has nonsensical SLA {v}"


def test_no_stalls_when_db_empty(tmp_path):
    from trading_bot.supervisor import _find_stalled_roles
    db = _setup_state_db(tmp_path)
    out = _find_stalled_roles(db, sla_map={"strategy_coach": 30})
    assert out == []


def test_finished_runs_never_flagged(tmp_path):
    """Roles that completed normally are not stalls — even if old."""
    from trading_bot.supervisor import _find_stalled_roles
    db = _setup_state_db(tmp_path)
    _insert_run(db, role_name="strategy_coach",
                started_seconds_ago=10000, finished=True)
    out = _find_stalled_roles(db, sla_map={"strategy_coach": 30})
    assert out == []


def test_in_flight_runs_under_threshold_not_flagged(tmp_path):
    """A role that started 30s ago with sla=60s (threshold = 120s) is
    NOT stalled — it's still within its expected runtime window."""
    from trading_bot.supervisor import _find_stalled_roles
    db = _setup_state_db(tmp_path)
    _insert_run(db, role_name="strategy_coach", started_seconds_ago=30)
    out = _find_stalled_roles(db, sla_map={"strategy_coach": 60})
    assert out == []


def test_in_flight_runs_over_threshold_flagged(tmp_path):
    """started 300s ago with sla=60s → threshold 120s → STALLED."""
    from trading_bot.supervisor import _find_stalled_roles
    db = _setup_state_db(tmp_path)
    _insert_run(db, role_name="strategy_coach", started_seconds_ago=300)
    out = _find_stalled_roles(db, sla_map={"strategy_coach": 60})
    assert len(out) == 1
    s = out[0]
    assert s["role_name"] == "strategy_coach"
    assert s["sla_seconds"] == 60
    assert s["threshold_seconds"] == 120
    assert s["age_seconds"] >= 300


def test_min_threshold_protects_short_sla_roles(tmp_path):
    """health_pulse has sla=5s; 2× = 10s would alert on any normal run.
    The min_threshold_seconds floor (default 120) prevents that."""
    from trading_bot.supervisor import _find_stalled_roles
    db = _setup_state_db(tmp_path)
    _insert_run(db, role_name="health_pulse", started_seconds_ago=60)
    out = _find_stalled_roles(db, sla_map={"health_pulse": 5})
    # 60s old, sla=5, 2*sla=10, but min_threshold=120 → not stalled
    assert out == []


def test_role_missing_from_sla_map_uses_default(tmp_path):
    """If a role isn't in the SLA map, the watchdog uses sla=60 default
    + 120s min threshold → only flags clearly-stuck roles."""
    from trading_bot.supervisor import _find_stalled_roles
    db = _setup_state_db(tmp_path)
    _insert_run(db, role_name="brand_new_role", started_seconds_ago=300)
    out = _find_stalled_roles(db, sla_map={})
    assert len(out) == 1
    assert out[0]["role_name"] == "brand_new_role"
    assert out[0]["sla_seconds"] == 60   # fallback default
