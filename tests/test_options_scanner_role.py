"""Smoke tests for the OptionsScannerRole daemon wiring (Phase 3).

The role chains four steps (breaker check → poll sources → roll-up →
scout debate) and is fail-soft per step. We verify:

  - Breaker hard-trip → role returns ``skipped`` early
  - Empty universe / failing fetcher → role still completes (no raise)
  - Successful tick produces audit rows
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.options.circuit_breaker import (
    TripDecision, TripReason, TripSeverity, trip,
)
from trading_bot.pipelines.options.state_db import (
    IntelCandidateOptions,
    IntelEventOptions,
    ScoutDebateRunOptions,
)
from trading_bot.roles.options_scanner import OptionsScannerRole, _load_universe
from trading_bot.shared.llm_transport import LlmResponse
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# _load_universe
# ---------------------------------------------------------------------------


def test_load_universe_falls_back_to_default_when_path_missing():
    syms = _load_universe(None)
    assert "AAPL" in syms and "MSFT" in syms


def test_load_universe_falls_back_when_file_missing():
    syms = _load_universe("/nonexistent/path.yaml")
    assert "AAPL" in syms


def test_load_universe_reads_yaml(tmp_path):
    p = tmp_path / "allowlist.yaml"
    p.write_text("symbols:\n  - aapl\n  - tsla\n")
    syms = _load_universe(str(p))
    assert syms == ["AAPL", "TSLA"]


# ---------------------------------------------------------------------------
# OptionsScannerRole behaviour
# ---------------------------------------------------------------------------


def test_role_skips_when_breaker_hard_tripped(engine, monkeypatch):
    """Hard-tripped breaker → no source polls, no roll-up, no debate."""
    decision = TripDecision(
        should_trip=True, reason=TripReason.VIX_SPIKE,
        severity=TripSeverity.HARD, state={"vix_level": 42.0},
    )
    trip(engine, decision=decision)

    role = OptionsScannerRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.outputs.get("skipped") is True
    # Nothing written
    with Session(engine) as session:
        assert session.query(IntelEventOptions).count() == 0
        assert session.query(IntelCandidateOptions).count() == 0


def _patch_sources(monkeypatch):
    """Monkey-patch the slow yfinance + FRED fetchers so the test stays
    offline. Earnings: AAPL gets earnings 14d out. CBOE SKEW: 130."""
    import trading_bot.pipelines.options.sources.earnings_calendar as ecal
    import trading_bot.pipelines.options.sources.cboe_skew as skew

    fixed_now = dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.timezone.utc)

    def _fake_earnings(symbol: str):
        return fixed_now + dt.timedelta(days=14) if symbol == "AAPL" else None

    def _fake_skew():
        return (130.0, fixed_now)

    monkeypatch.setattr(ecal, "_default_fetcher", _fake_earnings)
    monkeypatch.setattr(skew, "_default_fetcher", _fake_skew)


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


def test_role_completes_full_chain(engine, monkeypatch, tmp_path):
    """End-to-end happy path: poll → roll-up → scout debate writes audit row."""
    _patch_sources(monkeypatch)

    # Stub the LLM transport so the scout debate doesn't hit a real subprocess.
    transport = _MockTransport([
        {  # reviewer
            "skeptic_briefs": {"AAPL": "Hank: IV elevated, watch for crush."},
            "analyst_briefs": {"AAPL": "Sofia: catalyst is real, elevate."},
        },
        {  # judge
            "verdicts": [{
                "underlying": "AAPL", "verdict": "elevate",
                "confidence": "high", "reason": "earnings + IV anchored",
            }],
        },
    ])
    import trading_bot.pipelines.options.scout_debate as scout_mod
    monkeypatch.setattr(scout_mod, "get_transport", lambda **kw: transport)

    # Tight allowlist so only AAPL gets polled.
    allowlist = tmp_path / "allow.yaml"
    allowlist.write_text("symbols:\n  - AAPL\n")

    role = OptionsScannerRole(engine=engine)
    result = role.safe_run(ctx={"allowlist_path": str(allowlist)})

    assert result.outputs.get("completed") is True
    earnings_summary = result.outputs.get("earnings_calendar") or {}
    assert earnings_summary.get("written", 0) >= 1
    roll = result.outputs.get("roll_up") or {}
    assert roll.get("candidates_upserted", 0) >= 1

    # Audit row landed
    with Session(engine) as session:
        runs = session.query(ScoutDebateRunOptions).all()
    assert len(runs) == 1
    assert runs[0].underlying == "AAPL"
    assert runs[0].verdict == "elevate"


def test_role_completes_when_no_universe_yields_data(engine, monkeypatch, tmp_path):
    """No earnings + no SKEW reachable → role still finishes cleanly."""
    import trading_bot.pipelines.options.sources.earnings_calendar as ecal
    import trading_bot.pipelines.options.sources.cboe_skew as skew
    monkeypatch.setattr(ecal, "_default_fetcher", lambda s: None)
    monkeypatch.setattr(skew, "_default_fetcher", lambda: None)

    role = OptionsScannerRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.outputs.get("completed") is True
    assert (result.outputs.get("roll_up") or {}).get("candidates_upserted", 0) == 0
