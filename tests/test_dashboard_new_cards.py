"""Dashboard data-loaders + fragments for the new W1/W6 cards.

Two new cards on the local dashboard:
  - Decision Activity — aggregates the W1 ``decisions`` table (24h window)
  - Freshness — same audit the daily digest + midday snapshot run
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from trading_bot.dashboard.data import (
    DecisionActivityBlock, FreshnessBlock,
    _build_decision_activity, _build_freshness,
)
from trading_bot.state_db import Base, get_engine


@pytest.fixture
def state_db_with_decisions(tmp_path: Path):
    """Create a state.db with several decisions covering different actions."""
    db = tmp_path / "state.db"
    Base.metadata.create_all(get_engine(db))
    now = dt.datetime.now(dt.timezone.utc)

    rows = [
        ("placed_order", "rsi=64", now - dt.timedelta(hours=1)),
        ("rejected_by_risk", "per_trade_risk: 1.20% > 1.00%", now - dt.timedelta(hours=2)),
        ("rejected_by_risk", "per_trade_risk: 1.20% > 1.00%", now - dt.timedelta(hours=3)),
        ("hold", "rsi out of band", now - dt.timedelta(hours=4)),
        ("skipped_intel", "macro_shock", now - dt.timedelta(hours=5)),
        # Outside 24h window — should be filtered out
        ("hold", "stale", now - dt.timedelta(hours=48)),
    ]
    with sqlite3.connect(str(db)) as conn:
        for action, reason, ts in rows:
            conn.execute(
                "INSERT INTO decisions (decision_id, timestamp_utc, symbol, action, "
                "reason, strategy, regime, asset_class, risk_after_json, "
                "compliance_json, data_quality_json, execution_constraints_json, "
                "alerts_json, audit_json, entry_order_id, stop_loss_order_id) "
                "VALUES (?, ?, 'X', ?, ?, 'momentum', 'trending_up', 'stock', "
                "'{}', '{}', '{}', '{}', '[]', '{}', '', '')",
                (f"d_{ts.timestamp()}", ts.isoformat(), action, reason),
            )
    return db


class TestDecisionActivity:
    def test_aggregates_within_window(self, state_db_with_decisions: Path):
        errors: list[str] = []
        block = _build_decision_activity(str(state_db_with_decisions), errors)
        assert block is not None
        # 5 in-window rows, 1 out-of-window row excluded
        assert block.total == 5
        # Should pick the most-common action first (here: rejected_by_risk = 2)
        actions = dict(block.action_counts)
        assert actions["rejected_by_risk"] == 2
        assert actions["placed_order"] == 1
        assert actions["hold"] == 1
        assert actions["skipped_intel"] == 1

    def test_top_rejection_reasons(self, state_db_with_decisions: Path):
        errors: list[str] = []
        block = _build_decision_activity(str(state_db_with_decisions), errors)
        # placed_order excluded from rejection_reasons
        reasons = dict(block.top_rejection_reasons)
        assert reasons.get("per_trade_risk: 1.20% > 1.00%") == 2
        assert reasons.get("rsi out of band") == 1
        # placed_order's reason ('rsi=64') must NOT appear
        assert "rsi=64" not in reasons

    def test_empty_table_returns_block_with_zero_total(self, tmp_path: Path):
        db = tmp_path / "empty.db"
        Base.metadata.create_all(get_engine(db))
        block = _build_decision_activity(str(db), [])
        assert block is not None
        assert block.total == 0
        assert block.action_counts == []

    def test_missing_db_returns_none(self, tmp_path: Path):
        errors: list[str] = []
        # Point at a path that exists but has no decisions table
        bad = tmp_path / "no_table.db"
        sqlite3.connect(str(bad)).close()
        block = _build_decision_activity(str(bad), errors)
        # Should record the error and return None
        assert block is None
        assert errors


class TestFreshnessLoader:
    def test_returns_block_with_rows(self):
        block = _build_freshness([])
        assert block is not None
        assert isinstance(block, FreshnessBlock)
        assert all(r.severity in {"ok", "stale", "missing"} for r in block.rows)
        assert block.worst in {"ok", "stale", "missing"}


class TestFragments:
    # Card titles were rewritten in plain English in Apr 2026 — these
    # assertions track the new copy.
    def test_decision_activity_fragment_renders(self):
        from trading_bot.dashboard.app import create_app
        app = create_app()
        client = TestClient(app)
        r = client.get("/fragment/decision_activity")
        assert r.status_code == 200
        assert "Decision Log" in r.text

    def test_freshness_fragment_renders(self):
        from trading_bot.dashboard.app import create_app
        app = create_app()
        client = TestClient(app)
        r = client.get("/fragment/freshness")
        assert r.status_code == 200
        assert "Data Health" in r.text

    def test_full_page_includes_new_cards(self):
        from trading_bot.dashboard.app import create_app
        app = create_app()
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200
        assert 'id="decision_activity"' in r.text
        assert 'id="freshness"' in r.text
        # Sidebar nav links present
        assert "Decision log" in r.text
        assert "Data health" in r.text
