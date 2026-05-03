"""Tests for the options wheel-entry debate (Phase 3).

Mocks the LLM transport and asserts:
  - three-reviewer + judge orchestration produces audit rows
  - executor is invoked only on `place` verdicts
  - judge's chosen_delta / chosen_dte_days / chosen_structure are
    recorded; defaults fall through to the proposed values when null
  - skip / defer_restale verdicts skip the executor and just audit
  - rate-limit returns gracefully without writing audit rows
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.state_db import WheelDebateRunOptions
from trading_bot.pipelines.options.wheel_debate import (
    WheelCandidate,
    WheelOrderExecutor,
    WheelVerdict,
    run_wheel_debate,
)
from trading_bot.shared.llm_transport import (
    LlmResponse,
    SubscriptionRateLimited,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class _MockTransport:
    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)

    def complete_structured(self, **kwargs: Any) -> LlmResponse:
        if not self._responses:
            raise AssertionError("MockTransport: no more canned responses")
        payload = self._responses.pop(0)
        return LlmResponse(
            text=json.dumps(payload),
            input_tokens=0, output_tokens=0, model="mock",
            raw={"result": payload},
        )


class _RateLimitedTransport:
    def complete_structured(self, **kwargs: Any) -> LlmResponse:
        raise SubscriptionRateLimited("rate limited")


class _RecordingExecutor:
    def __init__(self, broker_id: str = "wheel-order-1", cycle_id: int = 42) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.broker_id = broker_id
        self.cycle_id = cycle_id

    def submit_wheel_entry(self, **kwargs: Any):
        self.calls.append(kwargs)
        return (self.broker_id, self.cycle_id)


class _RaisingExecutor:
    def submit_wheel_entry(self, **kwargs: Any):
        raise RuntimeError("broker boom")


# ---------------------------------------------------------------------------
# Happy-path: place verdict invokes executor + writes audit
# ---------------------------------------------------------------------------


def _aapl_candidate() -> WheelCandidate:
    return WheelCandidate(
        underlying="AAPL",
        candidate_score=8.0, iv_rank=55.0,
        intel_top_reason="post-earnings IV anchored",
        sentiment_avg=0.3,
        proposed_strike=180.0, proposed_delta=0.20,
        proposed_dte_days=35, proposed_structure="csp",
    )


def test_place_verdict_invokes_executor_and_audits(engine):
    transport = _MockTransport([
        {  # reviewer call
            "aggressive_briefs":   {"AAPL": "Aurelio: tight setup, push 0.30 delta."},
            "conservative_briefs": {"AAPL": "Beatrice: 0.20 sufficient income."},
            "neutral_briefs":      {"AAPL": "Yusuf: VIX 18 — lean Beatrice, 0.22."},
        },
        {  # judge call
            "verdicts": [{
                "underlying": "AAPL", "verdict": "place",
                "confidence": "high",
                "reason": "place (high): 0.22 delta + 35d DTE per Yusuf's macro",
                "chosen_delta": 0.22, "chosen_dte_days": 35,
                "chosen_structure": "csp",
            }],
        },
    ])
    executor = _RecordingExecutor()
    result = run_wheel_debate(
        engine, candidates=[_aapl_candidate()], regime="neutral_vol",
        executor=executor, transport=transport, lessons_block="(no lessons)",
    )
    assert result.placed == 1
    assert result.skipped == 0
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call["chosen_delta"] == 0.22
    assert call["chosen_dte_days"] == 35
    assert call["chosen_structure"] == "csp"

    with Session(engine) as session:
        rows = session.query(WheelDebateRunOptions).all()
    assert len(rows) == 1
    audit = rows[0]
    assert audit.underlying == "AAPL"
    assert audit.verdict == "place"
    assert audit.chosen_delta == 0.22
    assert audit.chosen_dte_days == 35
    assert audit.entry_order_id == "wheel-order-1"
    assert audit.cycle_id == 42


def test_place_verdict_uses_proposed_when_judge_omits_chosen(engine):
    """If the judge returns place but doesn't include chosen_delta etc.,
    the runner falls back to the proposed values from the candidate."""
    transport = _MockTransport([
        {
            "aggressive_briefs":   {"AAPL": "ok"},
            "conservative_briefs": {"AAPL": "ok"},
            "neutral_briefs":      {"AAPL": "ok"},
        },
        {
            "verdicts": [{
                "underlying": "AAPL", "verdict": "place",
                "confidence": "medium", "reason": "ok",
                # chosen_* omitted entirely
            }],
        },
    ])
    executor = _RecordingExecutor()
    result = run_wheel_debate(
        engine, candidates=[_aapl_candidate()], executor=executor,
        transport=transport, lessons_block="(no lessons)",
    )
    assert result.placed == 1
    call = executor.calls[0]
    assert call["chosen_delta"] == 0.20  # falls back to proposed
    assert call["chosen_dte_days"] == 35
    assert call["chosen_structure"] == "csp"


def test_skip_verdict_does_not_invoke_executor(engine):
    transport = _MockTransport([
        {
            "aggressive_briefs":   {"AAPL": "ok"},
            "conservative_briefs": {"AAPL": "concern"},
            "neutral_briefs":      {"AAPL": "concern"},
        },
        {
            "verdicts": [{
                "underlying": "AAPL", "verdict": "skip",
                "confidence": "high", "reason": "earnings within DTE window",
            }],
        },
    ])
    executor = _RecordingExecutor()
    result = run_wheel_debate(
        engine, candidates=[_aapl_candidate()], executor=executor,
        transport=transport, lessons_block="(no lessons)",
    )
    assert result.skipped == 1
    assert result.placed == 0
    assert len(executor.calls) == 0
    with Session(engine) as session:
        audit = session.query(WheelDebateRunOptions).one()
    assert audit.verdict == "skip"
    assert audit.entry_order_id is None
    assert audit.cycle_id is None


def test_defer_restale_audit_only(engine):
    transport = _MockTransport([
        {
            "aggressive_briefs":   {"AAPL": "ok"},
            "conservative_briefs": {"AAPL": "ok"},
            "neutral_briefs":      {"AAPL": "ok"},
        },
        {
            "verdicts": [{
                "underlying": "AAPL", "verdict": "defer_restale",
                "confidence": "low", "reason": "IV dropped mid-debate",
            }],
        },
    ])
    executor = _RecordingExecutor()
    result = run_wheel_debate(
        engine, candidates=[_aapl_candidate()], executor=executor,
        transport=transport, lessons_block="(no lessons)",
    )
    assert result.deferred == 1
    assert len(executor.calls) == 0


def test_executor_exception_does_not_crash_audit(engine):
    transport = _MockTransport([
        {
            "aggressive_briefs":   {"AAPL": "ok"},
            "conservative_briefs": {"AAPL": "ok"},
            "neutral_briefs":      {"AAPL": "ok"},
        },
        {
            "verdicts": [{
                "underlying": "AAPL", "verdict": "place",
                "confidence": "high", "reason": "ok",
                "chosen_delta": 0.22, "chosen_dte_days": 35,
                "chosen_structure": "csp",
            }],
        },
    ])
    result = run_wheel_debate(
        engine, candidates=[_aapl_candidate()], executor=_RaisingExecutor(),
        transport=transport, lessons_block="(no lessons)",
    )
    # Audit row still written; entry_order_id is None
    assert result.placed == 1
    with Session(engine) as session:
        audit = session.query(WheelDebateRunOptions).one()
    assert audit.entry_order_id is None
    assert audit.cycle_id is None


def test_rate_limit_returns_gracefully_no_audit(engine):
    result = run_wheel_debate(
        engine, candidates=[_aapl_candidate()],
        executor=_RecordingExecutor(),
        transport=_RateLimitedTransport(),
        lessons_block="(no lessons)",
    )
    assert result.error == "rate_limited"
    assert result.placed == 0
    with Session(engine) as session:
        rows = session.query(WheelDebateRunOptions).all()
    assert rows == []


def test_empty_candidates_no_op(engine):
    result = run_wheel_debate(engine, candidates=[], transport=None)
    assert result.debated == 0
