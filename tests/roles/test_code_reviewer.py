"""CodeReviewerRole tests."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.roles.code_reviewer import CodeReviewerRole
from trading_bot.state_db import Base, TemplateProposal


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _add_pending(session, *, name: str, code: str, tests: str) -> int:
    p = TemplateProposal(
        proposed_at=dt.datetime.now(dt.timezone.utc),
        name=name,
        rationale="r",
        expected_regime="trending_up",
        code=code,
        tests=tests,
        params_to_search_json="{}",
        review_status="pending",
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p.id


def test_no_pending_proposals_returns_zero(engine):
    role = CodeReviewerRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.outputs == {"reviewed": 0, "accepted": 0, "rejected": 0}


def test_clean_proposal_accepted(engine, tmp_path, monkeypatch):
    """Use chdir so _evolved/_archive go under tmp_path, not the repo."""
    monkeypatch.chdir(tmp_path)
    code = """import math
def add(a, b): return a + b
"""
    tests = """import sys; sys.path.insert(0, '.')
from clean import add
def test_add(): assert add(2, 3) == 5
"""
    role = CodeReviewerRole(engine=engine)
    with Session(engine) as s:
        _add_pending(s, name="clean", code=code, tests=tests)
    result = role.safe_run(ctx={})
    assert result.outputs["reviewed"] == 1
    assert result.outputs["accepted"] == 1
    with Session(engine) as s:
        row = s.query(TemplateProposal).first()
    assert row.review_status == "accepted"
    assert row.accepted_at is not None
    # Files materialized
    assert (tmp_path / "src/trading_bot/strategies/_evolved/clean/clean.py").exists()


def test_proposal_with_forbidden_import_rejected(engine, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = """import os
def add(a, b): return a + b
"""
    tests = "def test_smoke(): assert True"
    role = CodeReviewerRole(engine=engine)
    with Session(engine) as s:
        _add_pending(s, name="bad_import", code=code, tests=tests)
    result = role.safe_run(ctx={})
    assert result.outputs["accepted"] == 0
    assert result.outputs["rejected"] == 1
    with Session(engine) as s:
        row = s.query(TemplateProposal).first()
    assert row.review_status == "rejected"
    assert (tmp_path / "src/trading_bot/strategies/_archive/bad_import").exists()


def test_proposal_with_failing_test_rejected(engine, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = "def add(a, b): return a + b + 1  # bug"
    tests = """import sys; sys.path.insert(0, '.')
from buggy import add
def test_add(): assert add(2, 3) == 5
"""
    role = CodeReviewerRole(engine=engine)
    with Session(engine) as s:
        _add_pending(s, name="buggy", code=code, tests=tests)
    result = role.safe_run(ctx={})
    assert result.outputs["accepted"] == 0
    assert result.outputs["rejected"] == 1
