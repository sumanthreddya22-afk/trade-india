"""Operator strategy submission — draft mode writes a strategy_version row."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from trading_bot.operator import controls


@pytest.fixture()
def ledger(tmp_path) -> Path:
    p = tmp_path / "ledger.db"
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def test_draft_submission_registers_strategy(ledger):
    out = controls.strategy_submit(
        name="My Mean Reverse",
        description="Buy when 20d z-score < -2.0, exit at 0; SPY universe.",
        mode="draft", operator="tester", ledger_db=ledger,
    )
    assert out["ok"]
    assert out["strategy_id"].startswith("MY_MEAN_REVERSE_")
    assert out["strategy_ver"] == 1
    assert out["mode"] == "draft"

    listed = controls.strategy_list(ledger_db=ledger)
    assert any(s["strategy_id"] == out["strategy_id"] for s in listed)
    matched = next(s for s in listed if s["strategy_id"] == out["strategy_id"])
    assert matched["status"] == "research_only"


def test_intake_mode_gated_when_llm_hotpath_disabled(ledger, monkeypatch):
    monkeypatch.delenv("TRADING_BOT_ENABLE_LLM_HOTPATH", raising=False)
    out = controls.strategy_submit(
        name="GatedHypothesis", description="x", mode="intake",
        operator="tester", ledger_db=ledger,
    )
    assert out["ok"]
    assert out.get("gated") is True
    assert "LLM hot-path disabled" in out["reason"]


def test_mutate_mode_gated_when_env_unset(ledger, monkeypatch):
    monkeypatch.setenv("TRADING_BOT_ENABLE_LLM_HOTPATH", "1")
    monkeypatch.delenv("TRADING_BOT_ENABLE_MUTATION_CYCLE", raising=False)
    out = controls.strategy_submit(
        name="MutateHyp", description="x", mode="mutate",
        operator="tester", ledger_db=ledger,
    )
    assert out["ok"]
    assert out.get("gated") is True
    assert "TRADING_BOT_ENABLE_MUTATION_CYCLE" in out["reason"]


def test_unknown_mode_rejected(ledger):
    with pytest.raises(ValueError):
        controls.strategy_submit(
            name="Bad", description="x", mode="explode",
            operator="tester", ledger_db=ledger,
        )
