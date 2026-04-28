"""cost_tracker tests."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.cost_tracker import (
    is_halted,
    monthly_spend,
    record_call,
)
from trading_bot.state_db import AnthropicCostLog, Base, CostHalt


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def test_record_call_writes_row(engine):
    with Session(engine) as s:
        cost = record_call(
            s,
            role_name="strategy_architect",
            model="claude-opus-4-7",
            input_tokens=1000,
            output_tokens=500,
        )
    # 1k * 15 + 0.5k * 75 = 0.015 + 0.0375 = $0.0525
    assert abs(cost - 0.0525) < 1e-6


def test_monthly_spend_aggregates(engine):
    with Session(engine) as s:
        for _ in range(3):
            record_call(
                s,
                role_name="r",
                model="claude-opus-4-7",
                input_tokens=10_000,
                output_tokens=10_000,
            )
        spend = monthly_spend(s)
    # 3 calls * (10000 * 15 + 10000 * 75) / 1e6 = 3 * 0.9 = 2.7
    assert abs(spend - 2.7) < 1e-6


def test_halt_when_cap_exceeded(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MONTHLY_BUDGET_USD", "0.10")
    with Session(engine) as s:
        # Spend $0.0525 — under cap, no halt
        record_call(
            s, role_name="r", model="claude-opus-4-7",
            input_tokens=1000, output_tokens=500,
        )
        assert not is_halted(s)
        # Second call pushes total to $0.105 — over cap, halt expected
        record_call(
            s, role_name="r", model="claude-opus-4-7",
            input_tokens=1000, output_tokens=500,
        )
        assert is_halted(s)


def test_unknown_model_falls_back_to_opus_pricing(engine):
    with Session(engine) as s:
        cost = record_call(
            s, role_name="r", model="some-future-model",
            input_tokens=1000, output_tokens=0,
        )
    assert abs(cost - 0.015) < 1e-6  # $15/M * 1k = $0.015
