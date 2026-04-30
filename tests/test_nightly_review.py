"""Bucket G: tests for the nightly self-review loop."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from trading_bot.nightly_review import (
    DriftFinding,
    NightlyReview,
    RiskSnapshot,
    SystemHealth,
    DecisionRollup,
    _drift_findings,
    _decision_rollup,
    _system_health,
    compose_email,
    gather_review,
    run_nightly_review,
)
from trading_bot.state_db import Base


@pytest.fixture
def engine(tmp_path: Path):
    e = create_engine(f"sqlite:///{tmp_path/'r.db'}")
    Base.metadata.create_all(e)
    return e


_decision_seq = [0]


def _add_decision(engine, *, ts: dt.datetime, action: str, strategy: str = "momentum",
                  reason: str = "", symbol: str = "AAPL"):
    _decision_seq[0] += 1
    decision_id = f"d_{_decision_seq[0]:06d}_{action[:8]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions "
            "(decision_id, timestamp_utc, symbol, action, reason, strategy, "
            " regime, asset_class, confidence, expected_edge_bps, "
            " risk_after_json, compliance_json, data_quality_json, "
            " execution_constraints_json, alerts_json, audit_json, "
            " entry_order_id, stop_loss_order_id) "
            "VALUES (:id, :ts, :sym, :act, :rsn, :strat, 'trending_up', 'us_equity', "
            "  null, null, '{}', '{}', '{}', '{}', '[]', '{}', '', '')"
        ), {"id": decision_id, "ts": ts, "sym": symbol,
            "act": action, "rsn": reason, "strat": strategy})


def test_decision_rollup_counts_today_actions(engine):
    """Bucket G: rollup tallies placed/rejected/held for the audit date."""
    today = dt.date(2026, 4, 30)
    today_dt = dt.datetime(2026, 4, 30, 12, tzinfo=dt.timezone.utc)
    yesterday_dt = dt.datetime(2026, 4, 29, 12, tzinfo=dt.timezone.utc)
    _add_decision(engine, ts=today_dt, action="placed_order")
    _add_decision(engine, ts=today_dt, action="placed_order")
    _add_decision(engine, ts=today_dt, action="rejected_by_risk")
    _add_decision(engine, ts=today_dt, action="rejected_by_gate", reason="earnings_in_window")
    _add_decision(engine, ts=today_dt, action="held")
    # Yesterday's row should NOT be counted
    _add_decision(engine, ts=yesterday_dt, action="placed_order")

    start = dt.datetime.combine(today, dt.time.min, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    rollup = _decision_rollup(engine, day_start=start, day_end=end)
    assert rollup.placed_order == 2
    assert rollup.rejected_by_risk == 1
    assert rollup.rejected_by_gate == 1
    assert rollup.held == 1


def test_drift_findings_flags_large_pp_change(engine):
    """A gate that blocks 10% of decisions on the 7-day baseline but 80%
    today shows up as a drift finding."""
    audit_date = dt.date(2026, 4, 30)
    today = dt.datetime.combine(audit_date, dt.time(12), tzinfo=dt.timezone.utc)

    # Today: 10 decisions, 8 of them blocked by sentiment gate (80%)
    for _ in range(8):
        _add_decision(engine, ts=today, action="rejected_by_gate", reason="skipped_sentiment")
    for _ in range(2):
        _add_decision(engine, ts=today, action="placed_order")

    # Baseline (3 days back): 100 decisions, 10 of them sentiment-blocked (10%)
    baseline = today - dt.timedelta(days=3)
    for _ in range(10):
        _add_decision(engine, ts=baseline, action="rejected_by_gate", reason="skipped_sentiment")
    for _ in range(90):
        _add_decision(engine, ts=baseline, action="placed_order")

    findings = _drift_findings(engine, audit_date=audit_date)
    sentiment = next((f for f in findings if "sentiment" in f.gate), None)
    assert sentiment is not None
    # Today 80%, baseline 10% → +70pp drift, severity bad
    assert sentiment.delta_pp > 30
    assert sentiment.severity in {"warn", "bad"}


def test_drift_findings_quiet_when_no_change(engine):
    """No drift findings when today matches baseline."""
    audit_date = dt.date(2026, 4, 30)
    today = dt.datetime.combine(audit_date, dt.time(12), tzinfo=dt.timezone.utc)
    for _ in range(2):
        _add_decision(engine, ts=today, action="rejected_by_gate", reason="earnings_in_window")
    for _ in range(8):
        _add_decision(engine, ts=today, action="placed_order")

    baseline = today - dt.timedelta(days=3)
    for _ in range(2):
        _add_decision(engine, ts=baseline, action="rejected_by_gate", reason="earnings_in_window")
    for _ in range(8):
        _add_decision(engine, ts=baseline, action="placed_order")

    assert _drift_findings(engine, audit_date=audit_date) == []


def test_system_health_reads_heartbeat_and_eligible(engine, tmp_path):
    """Heartbeat age + wheel-eligible-set count are surfaced."""
    hb = tmp_path / "hb.json"
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)
    hb.write_text(json.dumps({"ts": ts.isoformat()}))
    # Insert 3 eligible rows
    with engine.begin() as c:
        for sym in ("AAPL", "MSFT", "GOOG"):
            c.execute(text(
                "INSERT INTO wheel_universe_cache "
                "(symbol, eligible, reason, cached_at) "
                "VALUES (:s, 1, 'ok', :t)"
            ), {"s": sym, "t": dt.datetime.now(dt.timezone.utc)})

    h = _system_health(engine, heartbeat_path=hb, pause_path=tmp_path / "pause.flag")
    assert h.wheel_eligible_count == 3
    assert h.pause_flag_set is False
    assert h.heartbeat_age_minutes is not None
    assert 2 < h.heartbeat_age_minutes < 4


def test_compose_email_marks_red_when_halted(engine):
    """The subject + risk row reflect halted=True."""
    review = NightlyReview(
        as_of=dt.datetime(2026, 4, 30, 21, 0, tzinfo=dt.timezone.utc),
        audit_date=dt.date(2026, 4, 30),
        decisions=DecisionRollup(0, 0, 0, 0, {}),
        drift=[],
        freshness_summary="Freshness audit:\n  ✓ all caches green",
        risk=RiskSnapshot(
            consecutive_losing_days=5, halted=True,
            halt_reason="5 consecutive losing days — circuit breaker",
            halted_strategies=(), size_multiplier="0.25",
        ),
        health=SystemHealth(
            heartbeat_age_minutes=1.2, pause_flag_set=False,
            wheel_eligible_count=312, open_alerts_pending=0,
        ),
    )
    subject, html = compose_email(review)
    assert "🔴" in subject
    assert "5 consecutive losing days" in html
    assert "0.25" in html


def test_compose_email_green_when_quiet(engine):
    """No drift, no halts, fresh data → green tag."""
    review = NightlyReview(
        as_of=dt.datetime(2026, 4, 30, 21, 0, tzinfo=dt.timezone.utc),
        audit_date=dt.date(2026, 4, 30),
        decisions=DecisionRollup(3, 0, 1, 0, {}),
        drift=[],
        freshness_summary="Freshness audit:\n  ✓ all caches green",
        risk=RiskSnapshot(0, False, "", (), "1"),
        health=SystemHealth(
            heartbeat_age_minutes=1.2, pause_flag_set=False,
            wheel_eligible_count=312, open_alerts_pending=0,
        ),
    )
    subject, _ = compose_email(review)
    assert "🟢" in subject


def test_run_nightly_review_calls_send_logged(engine, tmp_path, monkeypatch):
    """End-to-end: review is gathered + send_logged is invoked once."""
    sender = MagicMock()
    sender.send = MagicMock()
    # Stub the email log layer so we don't need a real EmailLogStore.
    monkeypatch.setattr(
        "trading_bot.nightly_review.send_logged",
        lambda **kw: sender.send(subject=kw["subject"], html_body=kw["html_body"]),
    )
    # Stub freshness so it doesn't try to open production DBs.
    monkeypatch.setattr(
        "trading_bot.nightly_review.audit_freshness", lambda: []
    )
    monkeypatch.setattr(
        "trading_bot.nightly_review.render_text_summary",
        lambda findings: "Freshness audit:\n  (stubbed)\nWorst: ok",
    )
    out = run_nightly_review(
        engine=engine, sender=sender, recipient="x@y.com",
        heartbeat_path=tmp_path / "no.json",
        pause_path=tmp_path / "no.flag",
    )
    assert isinstance(out, NightlyReview)
    sender.send.assert_called_once()
    args, kwargs = sender.send.call_args
    assert "Nightly Review" in kwargs["subject"]
