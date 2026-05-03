"""Tests for the Phase 3 wheel-entry runner — fuses legacy WheelLane
chain proposals with the new wheel_debate runner.

We mock the broker chain fetch + LLM transport. The behaviour under
test is the orchestration: who gets debated, who gets skipped where,
how cycles open on place, and the audit chain end-to-end.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.options.chain import ChainContract
from trading_bot.options.wheel_lane import WheelDecision, WheelLane
from trading_bot.pipelines.options.state_db import (
    IntelCandidateOptions,
    WheelCycleOptions,
    WheelDebateRunOptions,
    WheelStateHistoryOptions,
)
from trading_bot.pipelines.options.wheel_runner import (
    WheelEntryDeps,
    _decision_to_candidate,
    run_wheel_entry,
    select_elevated_underlyings,
)
from trading_bot.shared.config import WheelConfig
from trading_bot.shared.llm_transport import LlmResponse
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_chain_contract(
    *, underlying: str, strike: float, delta: float,
    expiration: dt.date, kind: str = "P",
) -> ChainContract:
    return ChainContract(
        contract_symbol=f"{underlying}{expiration.strftime('%y%m%d')}{kind}{int(strike)}",
        underlying=underlying, expiration=expiration, kind=kind,
        strike=strike, bid=1.95, ask=2.05, last=2.0,
        volume=100, open_interest=500,
        implied_volatility=0.30, delta=delta,
    )


def _seed_elevated(
    engine, *, underlying: str, score: float = 6.0,
    iv_rank: float = 55.0, earnings_in_dte: bool = False,
    days_to_earn: Optional[int] = None,
    now: Optional[dt.datetime] = None,
) -> None:
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        session.add(IntelCandidateOptions(
            underlying=underlying,
            score=score, n_mentions=2, n_sources=2,
            first_seen=now - dt.timedelta(hours=24), last_seen=now,
            top_reason=f"{underlying} elevated by scout",
            sources_json='{"earnings_calendar": 5}',
            sentiment_avg=0.2, rolled_up_at=now,
            iv_rank=iv_rank,
            earnings_in_dte_window=earnings_in_dte,
            days_to_earnings=days_to_earn,
            scout_verdict="elevate",
        ))
        session.commit()


class _MockTransport:
    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)

    def complete_structured(self, **kwargs: Any) -> LlmResponse:
        if not self._responses:
            raise AssertionError("no more canned responses")
        payload = self._responses.pop(0)
        return LlmResponse(
            text=json.dumps(payload),
            input_tokens=0, output_tokens=0, model="mock",
            raw={"result": payload},
        )


def _wheel_cfg() -> WheelConfig:
    return WheelConfig(
        enabled=True,
        delta_target_low=0.20, delta_target_high=0.30,
        dte_min=30, dte_max=45,
        iv_rank_floor=30.0,
        vix_floor=10.0, vix_ceiling=35.0,
        sentiment_floor=-1.0,
        min_premium_abs=0.0, min_annualized_yield=0.0,
        min_open_interest=0,
        liquidity_max_spread_abs=10.0,
        liquidity_max_spread_rel=1.0,
        iv_rank_min_history=2,
    )


def _make_deps(
    engine,
    *,
    chain_for=None,
    spot=200.0,
    iv_rank=55.0,
    sentiment=0.2,
    regime="trending_up",
    vix=18.0,
    today: dt.date = dt.date(2026, 5, 4),
) -> WheelEntryDeps:
    lane = WheelLane(_wheel_cfg(), engine=engine)
    if chain_for is None:
        chain_for = lambda symbol: []
    return WheelEntryDeps(
        engine=engine, wheel_lane=lane,
        chain_for=chain_for,
        spot_for=lambda s: spot,
        iv_rank_for=lambda s: iv_rank,
        sentiment_for=lambda s: sentiment,
        regime_now=lambda: regime,
        vix_now=lambda: vix,
        today=lambda: today,
    )


# ---------------------------------------------------------------------------
# select_elevated_underlyings
# ---------------------------------------------------------------------------


def test_select_only_elevated_candidates(engine):
    _seed_elevated(engine, underlying="AAPL")
    # Non-elevated candidate must NOT show up.
    with Session(engine) as session:
        session.add(IntelCandidateOptions(
            underlying="TSLA", score=5.0, n_mentions=1, n_sources=1,
            first_seen=dt.datetime.now(dt.timezone.utc),
            last_seen=dt.datetime.now(dt.timezone.utc),
            sources_json="{}", rolled_up_at=dt.datetime.now(dt.timezone.utc),
            scout_verdict=None,
        ))
        session.commit()
    rows = select_elevated_underlyings(engine)
    assert [r.underlying for r in rows] == ["AAPL"]


def test_select_orders_by_score_desc(engine):
    _seed_elevated(engine, underlying="LOW", score=3.0)
    _seed_elevated(engine, underlying="HIGH", score=9.0)
    _seed_elevated(engine, underlying="MID", score=6.0)
    rows = select_elevated_underlyings(engine, batch_limit=10)
    assert [r.underlying for r in rows] == ["HIGH", "MID", "LOW"]


# ---------------------------------------------------------------------------
# _decision_to_candidate
# ---------------------------------------------------------------------------


def test_decision_to_candidate_csp_branch(engine):
    today = dt.date(2026, 5, 4)
    contract = _make_chain_contract(
        underlying="AAPL", strike=180.0, delta=-0.22,
        expiration=today + dt.timedelta(days=35),
    )
    decision = WheelDecision("open_csp", contract, "ok")
    cand_row = IntelCandidateOptions(
        underlying="AAPL", score=6.0, n_mentions=2, n_sources=2,
        first_seen=dt.datetime.now(dt.timezone.utc),
        last_seen=dt.datetime.now(dt.timezone.utc),
        sources_json="{}", rolled_up_at=dt.datetime.now(dt.timezone.utc),
        iv_rank=55.0, earnings_in_dte_window=False,
        top_reason="x",
    )
    out = _decision_to_candidate(candidate=cand_row, decision=decision, today=today)
    assert out is not None
    assert out.underlying == "AAPL"
    assert out.proposed_structure == "csp"
    assert out.proposed_strike == 180.0
    assert out.proposed_delta == 0.22  # unsigned for the prompt
    assert out.proposed_dte_days == 35


def test_decision_skip_returns_none(engine):
    decision = WheelDecision("skip", None, "iv_rank too low")
    cand_row = IntelCandidateOptions(
        underlying="AAPL", score=6.0, n_mentions=2, n_sources=2,
        first_seen=dt.datetime.now(dt.timezone.utc),
        last_seen=dt.datetime.now(dt.timezone.utc),
        sources_json="{}", rolled_up_at=dt.datetime.now(dt.timezone.utc),
        iv_rank=10.0, earnings_in_dte_window=False, top_reason="x",
    )
    assert _decision_to_candidate(
        candidate=cand_row, decision=decision, today=dt.date.today(),
    ) is None


# ---------------------------------------------------------------------------
# run_wheel_entry — orchestration
# ---------------------------------------------------------------------------


def test_no_elevated_candidates_returns_zero(engine):
    deps = _make_deps(engine)
    result = run_wheel_entry(deps, transport=None)
    assert result.debated == 0
    assert result.placed == 0


def test_lane_skip_bypasses_debate(engine, monkeypatch):
    """When WheelLane skips a candidate, the LLM debate must NOT be invoked."""
    today = dt.date(2026, 5, 4)
    _seed_elevated(engine, underlying="AAPL")

    # Provide a non-empty chain so we get past the data-availability
    # guard and into the lane proper. Then force the lane to skip.
    chain = [_make_chain_contract(
        underlying="AAPL", strike=180.0, delta=-0.22,
        expiration=today + dt.timedelta(days=35),
    )]
    deps = _make_deps(engine, chain_for=lambda s: chain, today=today)

    # Spy on the lane to confirm it was called.
    calls = []
    def _spy(inp):
        calls.append(inp.symbol)
        return WheelDecision("skip", None, "lane preflight rejected")
    monkeypatch.setattr(deps.wheel_lane, "evaluate", _spy)

    # No transport supplied — if the runner ever reaches the debate, this
    # will explode.
    result = run_wheel_entry(deps, transport=None, now=dt.datetime(2026, 5, 4, tzinfo=dt.timezone.utc))
    assert calls == ["AAPL"]
    assert result.skipped_in_lane == 1
    assert result.debated == 0


def test_full_chain_lane_to_place_opens_cycle(engine, monkeypatch):
    """End-to-end: elevated → lane proposes → debate places → cycle anchored."""
    today = dt.date(2026, 5, 4)
    now = dt.datetime(2026, 5, 4, 12, tzinfo=dt.timezone.utc)
    _seed_elevated(engine, underlying="AAPL")

    chain = [
        _make_chain_contract(
            underlying="AAPL", strike=180.0, delta=-0.22,
            expiration=today + dt.timedelta(days=35),
        ),
    ]
    deps = _make_deps(engine, chain_for=lambda s: chain, today=today, regime="trending_up")

    # Mock transport: reviewer call + judge place verdict.
    transport = _MockTransport([
        {
            "aggressive_briefs":   {"AAPL": "Aurelio: tight setup."},
            "conservative_briefs": {"AAPL": "Beatrice: 0.22 ok."},
            "neutral_briefs":      {"AAPL": "Yusuf: VIX 18, lean Beatrice."},
        },
        {
            "verdicts": [{
                "underlying": "AAPL", "verdict": "place",
                "confidence": "high",
                "reason": "place high: 0.22 delta + 35d DTE",
                "chosen_delta": 0.22, "chosen_dte_days": 35,
                "chosen_structure": "csp",
            }],
        },
    ])

    # A simple recording executor so we can assert the broker submit
    # call landed AND verify the cycle id round-trips.
    recorded = []
    class _RecExec:
        def submit_wheel_entry(self, **kw):
            recorded.append(kw)
            return ("broker-1", None)  # cycle_id ignored — runner provides

    result = run_wheel_entry(
        deps, executor=_RecExec(), transport=transport, now=now,
    )
    assert result.debated == 1
    assert result.placed == 1
    assert len(recorded) == 1
    assert recorded[0]["chosen_delta"] == 0.22
    assert recorded[0]["chosen_structure"] == "csp"

    # Cycle was opened in CSP_OPEN state with audit history.
    with Session(engine) as session:
        cycles = session.query(WheelCycleOptions).all()
        history = session.query(WheelStateHistoryOptions).all()
        runs = session.query(WheelDebateRunOptions).all()
    assert len(cycles) == 1
    assert cycles[0].underlying == "AAPL"
    assert cycles[0].state == WheelState_value()  # csp_open
    assert len(history) == 1
    assert history[0].from_state == "cash"
    assert history[0].to_state == "csp_open"
    assert len(runs) == 1
    assert runs[0].verdict == "place"
    # Cycle id was anchored from the runner's wrapper, not the inner executor.
    assert runs[0].cycle_id == cycles[0].id


def WheelState_value() -> str:
    """Helper returning the CSP_OPEN string value (avoids enum import in test body)."""
    from trading_bot.pipelines.options.wheel_state import WheelState
    return WheelState.CSP_OPEN.value


def test_skip_verdict_does_not_open_cycle(engine, monkeypatch):
    today = dt.date(2026, 5, 4)
    _seed_elevated(engine, underlying="AAPL")
    chain = [
        _make_chain_contract(
            underlying="AAPL", strike=180.0, delta=-0.22,
            expiration=today + dt.timedelta(days=35),
        ),
    ]
    deps = _make_deps(engine, chain_for=lambda s: chain, today=today, regime="trending_up")
    transport = _MockTransport([
        {
            "aggressive_briefs":   {"AAPL": "ok"},
            "conservative_briefs": {"AAPL": "concern"},
            "neutral_briefs":      {"AAPL": "concern"},
        },
        {
            "verdicts": [{
                "underlying": "AAPL", "verdict": "skip",
                "confidence": "high", "reason": "earnings risk",
            }],
        },
    ])
    class _RecExec:
        def submit_wheel_entry(self, **kw):  # never called
            raise AssertionError("executor must not be invoked on skip")
            return (None, None)

    result = run_wheel_entry(
        deps, executor=_RecExec(), transport=transport,
        now=dt.datetime(2026, 5, 4, tzinfo=dt.timezone.utc),
    )
    assert result.skipped == 1
    with Session(engine) as session:
        assert session.query(WheelCycleOptions).count() == 0


def test_dry_run_no_executor(engine):
    """executor=None still produces audit rows but never opens a cycle."""
    today = dt.date(2026, 5, 4)
    _seed_elevated(engine, underlying="AAPL")
    chain = [
        _make_chain_contract(
            underlying="AAPL", strike=180.0, delta=-0.22,
            expiration=today + dt.timedelta(days=35),
        ),
    ]
    deps = _make_deps(engine, chain_for=lambda s: chain, today=today, regime="trending_up")
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
    result = run_wheel_entry(deps, executor=None, transport=transport)
    assert result.placed == 1
    with Session(engine) as session:
        # Dry-run: no cycle row written
        assert session.query(WheelCycleOptions).count() == 0
        # But audit row is captured
        assert session.query(WheelDebateRunOptions).count() == 1


def test_chain_fetch_failure_skips_in_lane(engine):
    _seed_elevated(engine, underlying="AAPL")

    def _bad_fetcher(symbol):
        raise RuntimeError("simulated chain fetch failure")

    deps = _make_deps(engine, chain_for=_bad_fetcher)
    result = run_wheel_entry(deps, executor=None, transport=None)
    assert result.skipped_in_lane == 1
    assert result.debated == 0


def test_no_spot_skips_in_lane(engine):
    _seed_elevated(engine, underlying="AAPL")
    deps = _make_deps(engine)
    deps = WheelEntryDeps(**{**deps.__dict__, "spot_for": lambda s: None})
    result = run_wheel_entry(deps, executor=None, transport=None)
    assert result.skipped_in_lane == 1
