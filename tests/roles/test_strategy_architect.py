"""StrategyArchitectRole tests."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.anthropic_client import AnthropicCredsMissingError, AnthropicResponse
from trading_bot.roles.strategy_architect import (
    StrategyArchitectRole,
    _parse_proposals,
)
from trading_bot.state_db import Base, TemplateProposal


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def test_parse_proposals_strict_json():
    text = json.dumps([
        {
            "name": "test_v1",
            "rationale": "r",
            "expected_regime": "trending_up",
            "code": "def evaluate(): pass",
            "tests": "def test_x(): pass",
            "params_to_search": {},
        }
    ])
    parsed = _parse_proposals(text)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "test_v1"


def test_parse_proposals_with_markdown_fences():
    text = "```json\n" + json.dumps([
        {"name": "x", "rationale": "r", "code": "c", "tests": "t"}
    ]) + "\n```"
    parsed = _parse_proposals(text)
    assert len(parsed) == 1


def test_parse_proposals_dict_wrapped_in_list():
    text = json.dumps({"name": "x", "rationale": "r", "code": "c", "tests": "t"})
    parsed = _parse_proposals(text)
    assert len(parsed) == 1


def test_parse_proposals_missing_keys_filtered():
    text = json.dumps([
        {"name": "x"},  # missing required keys
        {"name": "y", "rationale": "r", "code": "c", "tests": "t"},
    ])
    parsed = _parse_proposals(text)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "y"


def test_skipped_when_no_creds(engine, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    role = StrategyArchitectRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.outputs.get("skipped") is True
    assert result.outputs["reason"] == "no_anthropic_creds"


def test_writes_proposals_on_success(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE-test")

    fake_response = AnthropicResponse(
        text=json.dumps([
            {
                "name": "mean_reversion_v2",
                "rationale": "Exploits oversold bounces in sideways regimes.",
                "expected_regime": "sideways",
                "code": "def evaluate(s, ind, eq): pass",
                "tests": "def test_smoke(): assert True",
                "params_to_search": {"rsi_lower": [25, 35, "float"]},
            }
        ]),
        input_tokens=500,
        output_tokens=2000,
        request_id="req-123",
        model="claude-opus-4-7",
    )

    role = StrategyArchitectRole(engine=engine)
    with patch("trading_bot.roles.strategy_architect.AnthropicClient") as mock_cls:
        instance = MagicMock()
        instance.complete.return_value = fake_response
        mock_cls.return_value = instance
        result = role.safe_run(ctx={})

    assert result.outputs["n_proposals"] == 1
    assert result.outputs["names"] == ["mean_reversion_v2"]
    with Session(engine) as s:
        rows = s.query(TemplateProposal).all()
    assert len(rows) == 1
    assert rows[0].review_status == "pending"
