"""W6 — diagnose CLI tool tests.

Replays every gate's verdict on a single symbol on a given day. Reads the
``decisions`` table (W1.2) and produces a unified human-readable timeline.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.decisions_store import DecisionStore
from trading_bot.diagnose import build_symbol_timeline
from trading_bot.orchestrator import (
    AuditObject, ComplianceFlags, DataQualityFlags, Decision,
    ExecutionConstraints, RiskAfter,
)
from trading_bot.state_db import Base, get_engine


@pytest.fixture
def state_db(tmp_path: Path):
    db = tmp_path / "state.db"
    Base.metadata.create_all(get_engine(db))
    return db


@pytest.fixture
def store(state_db: Path):
    return DecisionStore(state_db)


def test_empty_db_returns_no_results(store: DecisionStore):
    timeline = build_symbol_timeline(store, "NVDA")
    assert timeline.entries == ()
    assert "NVDA" in timeline.summary


def test_returns_decisions_for_target_symbol(store: DecisionStore):
    for action, reason in [
        ("hold", "rsi out of band"),
        ("rejected_by_risk", "per_trade_risk: 1.20% > 1.00%"),
        ("placed_order", "rsi=62"),
    ]:
        store.append(
            Decision(symbol="NVDA", action=action, reason=reason),
            strategy="momentum", regime="trending_up", asset_class="us_equity",
        )
    # Decoy
    store.append(
        Decision(symbol="AAPL", action="hold", reason="not in cluster"),
        strategy="momentum", regime="trending_up", asset_class="us_equity",
    )

    timeline = build_symbol_timeline(store, "NVDA")
    actions = [e.action for e in timeline.entries]
    assert "rejected_by_risk" in actions
    assert "placed_order" in actions
    # AAPL decoy is not present
    symbols = {e.symbol for e in timeline.entries}
    assert symbols == {"NVDA"}


def test_timeline_includes_audit_metadata(store: DecisionStore):
    store.append(
        Decision(
            symbol="NVDA", action="placed_order", reason="momentum",
            audit=AuditObject(
                policy_version="abc_def",
                strategy_version="momentum:abc",
                regime="trending_up",
                timestamp_utc="2026-04-29T19:41:00Z",
            ),
        ),
        strategy="momentum", regime="trending_up", asset_class="us_equity",
    )
    timeline = build_symbol_timeline(store, "NVDA")
    assert any("policy_version" in e.audit_summary for e in timeline.entries)


def test_summary_string_renderable(store: DecisionStore):
    """A human-readable single-line summary that the CLI can print."""
    store.append(
        Decision(symbol="NVDA", action="rejected_by_risk", reason="vol_too_high"),
        strategy="momentum", regime="trending_up", asset_class="us_equity",
    )
    timeline = build_symbol_timeline(store, "NVDA")
    rendered = timeline.render()
    assert "NVDA" in rendered
    assert "rejected_by_risk" in rendered
    assert "vol_too_high" in rendered
