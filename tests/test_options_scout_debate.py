"""Tests for options scout debate (Phase 3).

Mocks the LLM transport — real Claude CLI subprocesses are NOT invoked.
Verifies the two-call orchestration, JSON parsing, verdict application
to IntelCandidateOptions, and the audit row write.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.scout_debate import (
    OptionsScoutVerdict,
    run_scout_debate,
    select_candidates,
)
from trading_bot.pipelines.options.state_db import (
    IntelCandidateOptions,
    ScoutDebateRunOptions,
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


def _seed_candidate(
    engine: Any,
    *,
    underlying: str,
    score: float,
    iv_rank: float = 60.0,
    earnings_in_dte_window: bool = False,
    days_to_earnings: Optional[int] = None,
    now: Optional[dt.datetime] = None,
) -> None:
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        session.add(IntelCandidateOptions(
            underlying=underlying,
            score=score,
            n_mentions=2,
            n_sources=2,
            first_seen=now - dt.timedelta(hours=24),
            last_seen=now,
            top_reason=f"{underlying} earnings beat (mock)",
            sources_json='{"earnings_calendar": 5.0}',
            sentiment_avg=0.2,
            rolled_up_at=now,
            iv_rank=iv_rank,
            earnings_in_dte_window=earnings_in_dte_window,
            days_to_earnings=days_to_earnings,
        ))
        session.commit()


# ---------------------------------------------------------------------------
# Mock transport
# ---------------------------------------------------------------------------


class _MockTransport:
    """Returns canned responses in order — first reviewer call, then judge."""
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
        raise SubscriptionRateLimited("simulated rate limit")


# ---------------------------------------------------------------------------
# select_candidates
# ---------------------------------------------------------------------------


def test_select_candidates_filters_by_threshold(engine):
    _seed_candidate(engine, underlying="AAPL", score=5.0)
    _seed_candidate(engine, underlying="TSLA", score=2.0)
    rows = select_candidates(engine, threshold=3.0, batch_limit=10)
    assert [r.underlying for r in rows] == ["AAPL"]


def test_select_candidates_orders_by_score_desc(engine):
    _seed_candidate(engine, underlying="AAPL", score=5.0)
    _seed_candidate(engine, underlying="MSFT", score=8.0)
    _seed_candidate(engine, underlying="GOOG", score=6.0)
    rows = select_candidates(engine, threshold=3.0, batch_limit=10)
    assert [r.underlying for r in rows] == ["MSFT", "GOOG", "AAPL"]


def test_select_candidates_excludes_dismissed(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    _seed_candidate(engine, underlying="AAPL", score=5.0, now=now)
    # Mark AAPL dismissed for 24h
    with Session(engine) as session:
        cand = session.query(IntelCandidateOptions).filter_by(underlying="AAPL").one()
        cand.scout_dismissed_until = now + dt.timedelta(hours=24)
        session.commit()
    rows = select_candidates(engine, threshold=3.0, batch_limit=10, now=now)
    assert rows == []
    # After dismissal expires, candidate is debatable again
    rows_later = select_candidates(
        engine, threshold=3.0, batch_limit=10,
        now=now + dt.timedelta(hours=25),
    )
    assert [r.underlying for r in rows_later] == ["AAPL"]


# ---------------------------------------------------------------------------
# run_scout_debate happy paths
# ---------------------------------------------------------------------------


def test_run_scout_debate_elevates_and_audits(engine):
    _seed_candidate(engine, underlying="AAPL", score=5.0, iv_rank=65.0)
    transport = _MockTransport([
        {  # reviewer call
            "skeptic_briefs": {"AAPL": "Hank: IV rank 65 looks anchored; cautious."},
            "analyst_briefs": {"AAPL": "Sofia: catalyst lined up; elevate-worthy."},
        },
        {  # judge call
            "verdicts": [{
                "underlying": "AAPL", "verdict": "elevate", "confidence": "high",
                "reason": "elevate (high): IV rank 65% + post-earnings catalyst",
            }],
        },
    ])
    result = run_scout_debate(
        engine, threshold=3.0, batch_limit=10, transport=transport,
        lessons_block="(no lessons)",
    )
    assert result.debated == 1
    assert result.elevated == 1
    assert result.dismissed == 0
    assert result.verdicts[0].verdict == "elevate"

    # Verify candidate row updated
    with Session(engine) as session:
        cand = session.query(IntelCandidateOptions).filter_by(underlying="AAPL").one()
        assert cand.scout_verdict == "elevate"
        assert cand.scout_dismissed_until is None

        audit_rows = session.query(ScoutDebateRunOptions).all()
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.underlying == "AAPL"
    assert audit.verdict == "elevate"
    assert "Hank" in audit.skeptic_text
    assert "Sofia" in audit.analyst_text


def test_run_scout_debate_dismisses_with_ttl(engine):
    now = dt.datetime(2026, 5, 3, tzinfo=dt.timezone.utc)
    _seed_candidate(engine, underlying="GME", score=5.0, iv_rank=85.0, now=now)
    transport = _MockTransport([
        {
            "skeptic_briefs": {"GME": "Hank: retail IV froth, no catalyst."},
            "analyst_briefs": {"GME": "Sofia: agree, dismiss."},
        },
        {
            "verdicts": [{
                "underlying": "GME", "verdict": "dismiss", "confidence": "high",
                "reason": "dismiss (high): retail-driven IV pump",
            }],
        },
    ])
    result = run_scout_debate(
        engine, threshold=3.0, batch_limit=10, dismiss_ttl_hours=24,
        transport=transport, now=now, lessons_block="(no lessons)",
    )
    assert result.dismissed == 1

    with Session(engine) as session:
        cand = session.query(IntelCandidateOptions).filter_by(underlying="GME").one()
        assert cand.scout_verdict == "dismiss"
        assert cand.scout_dismissed_until is not None
        # TTL is 24 hours from now. SQLite drops tz info on read; reattach UTC
        # before comparing so the delta math doesn't trip naive/aware mismatch.
        dismissed_until = cand.scout_dismissed_until
        if dismissed_until.tzinfo is None:
            dismissed_until = dismissed_until.replace(tzinfo=dt.timezone.utc)
        delta = dismissed_until - now
        assert dt.timedelta(hours=23) < delta < dt.timedelta(hours=25)


def test_run_scout_debate_empty_pool_no_op(engine):
    result = run_scout_debate(engine, threshold=3.0, batch_limit=10, transport=None)
    assert result.debated == 0
    assert result.elevated == 0


def test_run_scout_debate_rate_limited_skips(engine):
    _seed_candidate(engine, underlying="AAPL", score=5.0)
    result = run_scout_debate(
        engine, threshold=3.0, batch_limit=10,
        transport=_RateLimitedTransport(),
        lessons_block="(no lessons)",
    )
    assert result.error == "rate_limited"
    assert result.elevated == 0
    # Candidate should NOT be marked elevated/dismissed
    with Session(engine) as session:
        cand = session.query(IntelCandidateOptions).filter_by(underlying="AAPL").one()
        assert cand.scout_verdict is None


def test_run_scout_debate_unknown_symbol_in_verdict_logged_not_crashed(engine):
    """Judge produces verdict for symbol not in the candidate set — should
    skip that verdict cleanly without raising."""
    _seed_candidate(engine, underlying="AAPL", score=5.0)
    transport = _MockTransport([
        {
            "skeptic_briefs": {"AAPL": "Hank: ok."},
            "analyst_briefs": {"AAPL": "Sofia: ok."},
        },
        {
            "verdicts": [
                {"underlying": "AAPL", "verdict": "elevate",
                 "confidence": "high", "reason": "good"},
                {"underlying": "BOGUS", "verdict": "elevate",
                 "confidence": "high", "reason": "phantom"},
            ],
        },
    ])
    result = run_scout_debate(
        engine, threshold=3.0, batch_limit=10, transport=transport,
        lessons_block="(no lessons)",
    )
    # Only AAPL is real → 1 elevated; BOGUS verdict is ignored.
    assert result.elevated == 1
