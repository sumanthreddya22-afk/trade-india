"""Strategy submit mode=intake — real SubprocessPersonaRunner path.

We don't spawn a real ``claude --json`` (CI doesn't have one), so we
inject a fake subprocess command and assert the runner returns the
shape we expect. The test that the controller invokes the runner at
all is what matters here.
"""
from __future__ import annotations

import json
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


def test_intake_returns_persona_error_when_command_missing(
    ledger, monkeypatch,
):
    """If TRADING_BOT_PERSONA_CMD points at a non-existent binary, we
    get a clean status='error' with a 'fix' field — not a stack trace."""
    monkeypatch.setenv("TRADING_BOT_ENABLE_LLM_HOTPATH", "1")
    monkeypatch.setenv("TRADING_BOT_PERSONA_CMD", "this-binary-does-not-exist")
    out = controls.strategy_submit(
        name="TestIntake",
        description="z-score mean reversion on top-100 S&P names",
        mode="intake", operator="tester", ledger_db=ledger,
    )
    assert out["ok"]
    assert out["intake"]["status"] == "error"
    assert "fix" in out["intake"]
