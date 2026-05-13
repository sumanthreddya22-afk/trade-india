"""Phase 5 — adversarial-pair hypothesis intake."""
from __future__ import annotations

import datetime as dt

from trading_bot.registry import ensure_registry_tables, register_version
from trading_bot.research import (
    HypothesisProposal, MockPersonaRunner, run_intake,
)


def _proposal(thesis="edge_thesis_v1"):
    return HypothesisProposal(
        thesis_id=thesis, hypothesis_id=thesis,
        description="ETF time-series momentum, monthly rebalance.",
        mechanism="Behavioural premium from slow institutional reallocation.",
        expected_regimes=("trending",),
        kill_criteria=("24m rolling Sharpe < 0",),
        proposed_by="operator",
    )


def _seed_strategy(conn, sid="X"):
    ensure_registry_tables(conn)
    register_version(
        conn, strategy_id=sid, strategy_ver=1,
        code_hash="c", config_hash="cf", thesis_id="edge_thesis_v1",
        hypothesis_id="edge_thesis_v1", lane="etf_momentum", owner="op",
    )


def test_intake_accepted_when_both_support(ledger_conn) -> None:
    _seed_strategy(ledger_conn)
    res = run_intake(
        ledger_conn, strategy_id="X", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=MockPersonaRunner(
            role="quant_research_lead.v1", verdict="support", confidence=0.7,
        ),
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="support", confidence=0.6,
        ),
        policy_hash="ph", feature_snapshot_id="fs",
    )
    assert res.accepted
    cur = ledger_conn.cursor()
    cur.execute("SELECT risk_decision FROM strategy_decision")
    assert cur.fetchone()[0] == "accept"


def test_intake_blocked_when_validator_blocks_with_high_confidence(
    ledger_conn,
) -> None:
    _seed_strategy(ledger_conn)
    res = run_intake(
        ledger_conn, strategy_id="X", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=MockPersonaRunner(
            role="quant_research_lead.v1", verdict="support",
        ),
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="block", confidence=0.9,
        ),
        policy_hash="ph", feature_snapshot_id="fs",
    )
    assert not res.accepted
    assert "risk_validator block" in res.reason


def test_intake_allows_operator_override(ledger_conn) -> None:
    _seed_strategy(ledger_conn)
    res = run_intake(
        ledger_conn, strategy_id="X", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=MockPersonaRunner(
            role="quant_research_lead.v1", verdict="support",
        ),
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="block", confidence=0.9,
        ),
        policy_hash="ph", feature_snapshot_id="fs",
        operator_override=True,
    )
    assert res.accepted


def test_low_confidence_block_does_not_kill(ledger_conn) -> None:
    _seed_strategy(ledger_conn)
    res = run_intake(
        ledger_conn, strategy_id="X", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=MockPersonaRunner(
            role="quant_research_lead.v1", verdict="support",
        ),
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="block", confidence=0.5,
        ),
        policy_hash="ph", feature_snapshot_id="fs",
    )
    assert res.accepted


def test_invalid_schema_rejected(ledger_conn) -> None:
    _seed_strategy(ledger_conn)

    def bad_runner(proposal):
        # Missing required fields → schema fail.
        return {"role": "broken"}

    res = run_intake(
        ledger_conn, strategy_id="X", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=bad_runner,
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="support",
        ),
        policy_hash="ph", feature_snapshot_id="fs",
    )
    assert not res.accepted
    assert "schema invalid" in res.reason


def test_hypothesis_hash_is_deterministic() -> None:
    h1 = _proposal().hash()
    h2 = _proposal().hash()
    assert h1 == h2
    assert len(h1) == 64
