"""W1.1 — Decision schema enrichment.

Tests for the PDF-prescribed sub-objects on `orchestrator.Decision`:
risk_after, compliance, data_quality, execution_constraints, alerts, audit.

Backward compatibility is a hard requirement: every existing call site of
`Decision(symbol=..., action=...)` (≈12 in orchestrator.py + tests) must keep
working without code changes. Defaults must therefore produce a fully-valid
Decision.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from trading_bot.orchestrator import (
    AuditObject,
    ComplianceFlags,
    DataQualityFlags,
    Decision,
    ExecutionConstraints,
    RiskAfter,
    decision_to_dict,
)


class TestBackwardCompatibility:
    """Existing 5-arg Decision construction must keep working unchanged."""

    def test_minimal_construction(self):
        d = Decision(symbol="NVDA", action="hold")
        assert d.symbol == "NVDA"
        assert d.action == "hold"
        assert d.reason == ""
        assert d.entry_order_id == ""
        assert d.stop_loss_order_id == ""

    def test_legacy_full_construction(self):
        d = Decision(
            symbol="NVDA",
            action="placed_order",
            reason="rsi=62",
            entry_order_id="o-1",
            stop_loss_order_id="o-1-stop",
        )
        assert d.entry_order_id == "o-1"
        assert d.stop_loss_order_id == "o-1-stop"

    def test_decision_remains_frozen(self):
        d = Decision(symbol="NVDA", action="hold")
        with pytest.raises(Exception):  # FrozenInstanceError
            d.action = "buy"  # type: ignore[misc]


class TestNewSubObjectDefaults:
    """Defaults must produce non-None sub-objects with all fields = None or empty."""

    def test_risk_after_default_is_all_none(self):
        d = Decision(symbol="X", action="hold")
        assert isinstance(d.risk_after, RiskAfter)
        assert d.risk_after.trade_var is None
        assert d.risk_after.portfolio_var_after is None
        assert d.risk_after.expected_shortfall_after is None
        assert d.risk_after.gross_after is None
        assert d.risk_after.net_after is None
        assert d.risk_after.drawdown_state is None

    def test_compliance_default_is_all_none(self):
        d = Decision(symbol="X", action="hold")
        assert isinstance(d.compliance, ComplianceFlags)
        assert d.compliance.approved_instrument is None
        assert d.compliance.approved_venue is None
        assert d.compliance.restricted_list_clear is None
        assert d.compliance.mnpi_clear is None
        assert d.compliance.market_abuse_clear is None

    def test_data_quality_default_is_all_none(self):
        d = Decision(symbol="X", action="hold")
        assert isinstance(d.data_quality, DataQualityFlags)
        assert d.data_quality.fresh is None
        assert d.data_quality.complete is None
        assert d.data_quality.aligned is None
        assert d.data_quality.provenance_ok is None

    def test_execution_constraints_default_is_all_none(self):
        d = Decision(symbol="X", action="hold")
        assert isinstance(d.execution_constraints, ExecutionConstraints)
        assert d.execution_constraints.price_collar_ok is None
        assert d.execution_constraints.size_collar_ok is None
        assert d.execution_constraints.max_participation is None

    def test_alerts_default_is_empty_tuple(self):
        d = Decision(symbol="X", action="hold")
        assert d.alerts == ()

    def test_audit_default_is_empty_audit_object(self):
        d = Decision(symbol="X", action="hold")
        assert isinstance(d.audit, AuditObject)
        assert d.audit.policy_version == ""
        assert d.audit.strategy_version == ""
        assert d.audit.model_versions == {}
        assert d.audit.prompt_versions == {}
        assert d.audit.data_snapshot_ids == ()
        assert d.audit.regime == ""
        assert d.audit.risk_state_id == ""
        assert d.audit.timestamp_utc == ""

    def test_confidence_and_edge_default_to_none(self):
        d = Decision(symbol="X", action="hold")
        assert d.confidence is None
        assert d.expected_edge_bps is None


class TestFullyPopulatedDecision:
    """A fully-populated Decision can be constructed via keyword args."""

    def test_construct_with_all_fields(self):
        d = Decision(
            symbol="NVDA",
            action="placed_order",
            reason="momentum",
            entry_order_id="o-1",
            stop_loss_order_id="o-1-stop",
            confidence=0.72,
            expected_edge_bps=18.4,
            risk_after=RiskAfter(
                trade_var=Decimal("0.05"),
                portfolio_var_after=Decimal("0.94"),
                expected_shortfall_after=Decimal("1.08"),
                gross_after=Decimal("1.82"),
                net_after=Decimal("0.54"),
                drawdown_state=Decimal("0.63"),
            ),
            compliance=ComplianceFlags(
                approved_instrument=True,
                approved_venue=True,
                restricted_list_clear=True,
                mnpi_clear=True,
                market_abuse_clear=True,
            ),
            data_quality=DataQualityFlags(
                fresh=True, complete=True, aligned=True, provenance_ok=True,
            ),
            execution_constraints=ExecutionConstraints(
                price_collar_ok=True,
                size_collar_ok=True,
                max_participation=Decimal("0.10"),
            ),
            alerts=("risk_limit_headroom_low",),
            audit=AuditObject(
                policy_version="2026-04-prod-17",
                strategy_version="momentum_v3:abc123",
                model_versions={"strategy_architect": "claude-opus-4-7"},
                prompt_versions={"strategy_architect": "v1:def456"},
                data_snapshot_ids=("md_88421", "risk_21994"),
                regime="trending_up",
                risk_state_id="rs_1",
                timestamp_utc="2026-04-29T19:41:00Z",
            ),
        )
        assert d.confidence == 0.72
        assert d.risk_after.portfolio_var_after == Decimal("0.94")
        assert d.compliance.mnpi_clear is True
        assert d.alerts == ("risk_limit_headroom_low",)
        assert d.audit.model_versions == {"strategy_architect": "claude-opus-4-7"}


class TestDecisionToDict:
    """`decision_to_dict` produces a JSON-serializable dict that matches the
    PDF's strict JSON output contract (page 6, 14-16)."""

    def test_minimal_decision_serializes(self):
        d = Decision(symbol="NVDA", action="hold")
        out = decision_to_dict(d)
        assert out["symbol"] == "NVDA"
        assert out["action"] == "hold"
        # Sub-objects render as plain dicts even when empty
        assert isinstance(out["risk_after"], dict)
        assert isinstance(out["compliance"], dict)
        assert isinstance(out["data_quality"], dict)
        assert isinstance(out["execution_constraints"], dict)
        assert isinstance(out["alerts"], list)
        assert isinstance(out["audit"], dict)
        # Round-trip JSON
        roundtrip = json.loads(json.dumps(out))
        assert roundtrip == out

    def test_decimals_serialize_as_strings(self):
        """Decimal fields must serialize losslessly. JSON has no Decimal type
        so we use string representation — preserves precision and is what
        the PDF's example outputs do."""
        d = Decision(
            symbol="ESM6",
            action="no_trade",
            risk_after=RiskAfter(
                trade_var=Decimal("0.05"),
                portfolio_var_after=Decimal("0.94"),
            ),
        )
        out = decision_to_dict(d)
        assert out["risk_after"]["trade_var"] == "0.05"
        assert out["risk_after"]["portfolio_var_after"] == "0.94"
        # Round-trip JSON
        json.dumps(out)

    def test_full_pdf_example_shape(self):
        """Mirrors the PDF page 14-15 example output. Verifies our schema can
        round-trip the regulator-grounded contract."""
        d = Decision(
            symbol="ESM6",
            action="no_trade",
            reason=(
                "Trend is positive, but projected portfolio VaR breaches the "
                "hard limit after cost-adjusted sizing."
            ),
            confidence=0.67,
            expected_edge_bps=2.1,
            risk_after=RiskAfter(
                trade_var=Decimal("0.05"),
                portfolio_var_after=Decimal("0.94"),
                expected_shortfall_after=Decimal("1.08"),
                gross_after=Decimal("1.82"),
                net_after=Decimal("0.54"),
                drawdown_state=Decimal("0.63"),
            ),
            compliance=ComplianceFlags(
                approved_instrument=True, approved_venue=True,
                restricted_list_clear=True, mnpi_clear=True,
                market_abuse_clear=True,
            ),
            data_quality=DataQualityFlags(
                fresh=True, complete=True, aligned=True, provenance_ok=True,
            ),
            execution_constraints=ExecutionConstraints(
                price_collar_ok=True, size_collar_ok=True,
                max_participation=Decimal("0.10"),
            ),
            alerts=("risk_limit_headroom_low",),
            audit=AuditObject(
                policy_version="2026-04-prod-17",
                model_versions={"trend": "trend_v9", "risk": "risk_v4"},
                data_snapshot_ids=("md_88421", "risk_21994"),
                timestamp_utc="2026-04-29T19:41:00Z",
            ),
        )
        out = decision_to_dict(d)
        # Spot-check key fields documented in the PDF
        assert out["action"] == "no_trade"
        assert out["expected_edge_bps"] == 2.1
        assert out["confidence"] == 0.67
        assert out["risk_after"]["portfolio_var_after"] == "0.94"
        assert out["compliance"]["mnpi_clear"] is True
        assert out["data_quality"]["fresh"] is True
        assert out["alerts"] == ["risk_limit_headroom_low"]
        assert out["audit"]["policy_version"] == "2026-04-prod-17"
        assert out["audit"]["model_versions"] == {"trend": "trend_v9", "risk": "risk_v4"}
        assert out["audit"]["data_snapshot_ids"] == ["md_88421", "risk_21994"]
        # Final sanity: JSON round-trip
        roundtrip = json.loads(json.dumps(out))
        assert roundtrip["audit"]["timestamp_utc"] == "2026-04-29T19:41:00Z"
