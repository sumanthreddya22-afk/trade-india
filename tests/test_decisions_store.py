"""W1.2 — Decisions table append-only store.

The `decisions` table stores every decision the bot makes (placed, rejected,
skipped, escalated) with the full PDF-prescribed audit object. It is
append-only — only insert/select are exposed; no update or delete.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect

from trading_bot.decisions_store import DecisionStore
from trading_bot.orchestrator import (
    AuditObject,
    ComplianceFlags,
    DataQualityFlags,
    Decision,
    ExecutionConstraints,
    RiskAfter,
)
from trading_bot.state_db import Decisions, Base, get_engine


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    Base.metadata.create_all(get_engine(db))
    return db


@pytest.fixture
def store(state_db: Path) -> DecisionStore:
    return DecisionStore(state_db)


class TestSchema:
    def test_decisions_table_present(self, state_db: Path):
        engine = get_engine(state_db)
        names = inspect(engine).get_table_names()
        assert "decisions" in names

    def test_required_columns_present(self, state_db: Path):
        engine = get_engine(state_db)
        cols = {c["name"] for c in inspect(engine).get_columns("decisions")}
        for required in (
            "id", "decision_id", "timestamp_utc", "symbol", "action",
            "reason", "strategy", "regime", "asset_class",
            "confidence", "expected_edge_bps",
            "risk_after_json", "compliance_json", "data_quality_json",
            "execution_constraints_json", "alerts_json", "audit_json",
            "entry_order_id", "stop_loss_order_id",
        ):
            assert required in cols, f"missing column: {required}"


class TestAppendAndQuery:
    def test_append_minimal_decision(self, store: DecisionStore):
        d = Decision(symbol="NVDA", action="hold", reason="rsi out of band")
        decision_id = store.append(d, strategy="momentum", regime="trending_up", asset_class="us_equity")
        assert decision_id  # non-empty string

        rows = store.recent(limit=10)
        assert len(rows) == 1
        assert rows[0].symbol == "NVDA"
        assert rows[0].action == "hold"
        assert rows[0].decision_id == decision_id

    def test_append_full_decision_round_trips(self, store: DecisionStore):
        d = Decision(
            symbol="ESM6",
            action="no_trade",
            confidence=0.67,
            expected_edge_bps=2.1,
            risk_after=RiskAfter(
                trade_var=Decimal("0.05"),
                portfolio_var_after=Decimal("0.94"),
            ),
            compliance=ComplianceFlags(approved_instrument=True, mnpi_clear=True),
            data_quality=DataQualityFlags(fresh=True, complete=True, aligned=True, provenance_ok=True),
            execution_constraints=ExecutionConstraints(price_collar_ok=True),
            alerts=("risk_limit_headroom_low",),
            audit=AuditObject(
                policy_version="abc123",
                model_versions={"strategy_architect": "claude-opus-4-7"},
                data_snapshot_ids=("md_88421",),
                regime="trending_up",
                timestamp_utc="2026-04-29T19:41:00Z",
            ),
        )
        store.append(d, strategy="momentum", regime="trending_up", asset_class="us_equity")
        rows = store.recent(limit=10)
        assert len(rows) == 1
        # Round-trip the JSON columns
        risk_after = json.loads(rows[0].risk_after_json)
        assert risk_after["trade_var"] == "0.05"
        assert risk_after["portfolio_var_after"] == "0.94"
        compliance = json.loads(rows[0].compliance_json)
        assert compliance["mnpi_clear"] is True
        audit = json.loads(rows[0].audit_json)
        assert audit["policy_version"] == "abc123"
        assert audit["model_versions"] == {"strategy_architect": "claude-opus-4-7"}
        assert audit["data_snapshot_ids"] == ["md_88421"]

    def test_decision_id_is_unique(self, store: DecisionStore):
        d1 = Decision(symbol="A", action="hold")
        d2 = Decision(symbol="B", action="hold")
        id1 = store.append(d1, strategy="x", regime="r", asset_class="us_equity")
        id2 = store.append(d2, strategy="x", regime="r", asset_class="us_equity")
        assert id1 != id2

    def test_query_by_action(self, store: DecisionStore):
        for sym, action in [("A", "hold"), ("B", "rejected_by_risk"), ("C", "rejected_by_risk"), ("D", "placed_order")]:
            store.append(
                Decision(symbol=sym, action=action),
                strategy="x", regime="r", asset_class="us_equity",
            )
        counts = store.action_counts()
        assert counts["hold"] == 1
        assert counts["rejected_by_risk"] == 2
        assert counts["placed_order"] == 1


class TestAppendOnly:
    """The store is append-only — no update/delete API is exposed."""

    def test_no_update_method(self, store: DecisionStore):
        assert not hasattr(store, "update")

    def test_no_delete_method(self, store: DecisionStore):
        assert not hasattr(store, "delete")
