"""Smoke tests for the CryptoIntelIngestorRole — the role that closes
the orphan in the crypto pipeline (pipelines/crypto/sources had no
production caller until 2026-05-03)."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.circuit_breaker import (
    TripDecision, TripReason, TripSeverity, trip,
)
from trading_bot.pipelines.crypto.state_db import (
    IntelCandidateCrypto,
    IntelEventCrypto,
    ScoutDebateRunCrypto,
)
from trading_bot.roles.crypto_intel_ingestor import CryptoIntelIngestorRole
from trading_bot.shared.llm_transport import LlmResponse
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    # Eager import to register tables on Base.metadata
    import trading_bot.pipelines.crypto.state_db  # noqa: F401
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def test_role_skips_when_breaker_hard_tripped(engine, monkeypatch):
    """Hard-tripped breaker → no source polls, no roll-up, no debate."""
    decision = TripDecision(
        should_trip=True, reason=TripReason.BTC_CRASH,
        severity=TripSeverity.HARD, state={"btc_4h": -10.0},
    )
    trip(engine, decision=decision)

    role = CryptoIntelIngestorRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.outputs.get("skipped") is True

    with Session(engine) as session:
        assert session.query(IntelEventCrypto).count() == 0


def test_role_completes_when_no_keys_set(engine, monkeypatch):
    """All API-keyed sources skip silently; the role still finishes
    and reports zero events (no crash). This is the "default config"
    state — confirms the role is safe to register."""
    role = CryptoIntelIngestorRole(engine=engine)

    # Make collect_all return early per source so we don't hit the
    # network. Each source returns a SourceResult with skipped=1.
    from trading_bot.pipelines.crypto.sources._base import SourceResult
    fake_results = [SourceResult(source=name, skipped=1).as_dict()
                    for name in ("whale_alert", "coindesk_rss", "apewisdom_crypto")]

    import trading_bot.roles.crypto_intel_ingestor as mod
    monkeypatch.setattr(
        "trading_bot.pipelines.crypto.sources.collect_all",
        lambda *a, **kw: fake_results,
    )

    result = role.safe_run(ctx={})
    assert result.outputs.get("completed") is True
    assert result.outputs.get("events_written") == 0


def test_role_writes_events_then_runs_scout_debate(engine, monkeypatch):
    """End-to-end happy path: source writes 1 event → roll-up creates
    1 candidate → scout debate produces audit row."""
    from trading_bot.pipelines.crypto.sources._base import (
        SourceResult, write_event,
    )
    now = dt.datetime.now(dt.timezone.utc)

    # Stub collect_all to write a concrete event for ETH/USD via the
    # real write_event helper, so the aggregator has something to score.
    def _fake_collect_all(eng, *, settings, now=None):
        write_event(eng, symbol="ETH/USD", source="coindesk_rss",
                    headline="ETH ETF approved by SEC",
                    sentiment=0.8, raw_score=1.0,
                    event_at=now, event_hash="eth-rss-1", now=now)
        return [SourceResult(source="coindesk_rss", written=1).as_dict()]

    monkeypatch.setattr(
        "trading_bot.pipelines.crypto.sources.collect_all", _fake_collect_all,
    )

    # Stub the LLM transport so the scout debate doesn't hit a real
    # subprocess — assume the candidate gets elevated.
    class _MockTransport:
        def __init__(self):
            self.responses: List[Dict[str, Any]] = [
                {  # reviewer
                    "skeptic_briefs": {"ETH/USD": "ok"},
                    "analyst_briefs": {"ETH/USD": "ok"},
                },
                {  # judge
                    "verdicts": [{
                        "symbol": "ETH/USD", "verdict": "elevate",
                        "confidence": "high", "reason": "ok",
                    }],
                },
            ]
        def complete_structured(self, **kwargs):
            payload = self.responses.pop(0)
            return LlmResponse(text=json.dumps(payload), input_tokens=0,
                                output_tokens=0, model="mock",
                                raw={"result": payload})

    transport = _MockTransport()
    monkeypatch.setattr(
        "trading_bot.pipelines.crypto.scout_debate.get_transport",
        lambda **kw: transport,
    )

    role = CryptoIntelIngestorRole(engine=engine)
    result = role.safe_run(ctx={})

    assert result.outputs.get("completed") is True
    assert result.outputs.get("events_written") >= 1
    assert (result.outputs.get("roll_up") or {}).get("candidates_upserted") >= 1
    scout = result.outputs.get("scout_debate") or {}
    assert scout.get("debated") >= 1
    assert scout.get("elevated") >= 1

    with Session(engine) as session:
        runs = session.query(ScoutDebateRunCrypto).all()
        cands = session.query(IntelCandidateCrypto).all()
    assert len(runs) >= 1
    assert any(c.scout_verdict == "elevate" for c in cands)


def test_role_completes_when_collect_all_fails(engine, monkeypatch):
    """A failing source must not crash the role — roll-up and debate
    should still attempt to run."""
    def _crash(*args, **kwargs):
        raise RuntimeError("network is down")
    monkeypatch.setattr(
        "trading_bot.pipelines.crypto.sources.collect_all", _crash,
    )
    role = CryptoIntelIngestorRole(engine=engine)
    result = role.safe_run(ctx={})
    assert result.outputs.get("completed") is True
    assert result.outputs.get("events_written") == 0
    assert result.outputs.get("per_source") == []
