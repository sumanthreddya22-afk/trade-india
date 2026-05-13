"""Phase 5 — end-to-end research cycle driver."""
from __future__ import annotations

import datetime as dt
import random

from trading_bot.registry import ensure_registry_tables, register_version
from trading_bot.research import (
    HypothesisProposal, MockPersonaRunner, record_rejection, run_cycle,
)
from trading_bot.risk import load_policy


_BUNDLE = load_policy()
_VP = _BUNDLE.validation_policy


def _proposal():
    return HypothesisProposal(
        thesis_id="edge_thesis_v1", hypothesis_id="edge_thesis_v1",
        description="ETF time-series momentum.",
        mechanism="Behavioural premium.",
        expected_regimes=("trending",),
        kill_criteria=("24m rolling Sharpe < 0",),
        proposed_by="operator",
    )


def _seed_registry(conn, sid="ETF_MOM"):
    ensure_registry_tables(conn)
    register_version(
        conn, strategy_id=sid, strategy_ver=1,
        code_hash="c", config_hash="cf", thesis_id="edge_thesis_v1",
        hypothesis_id="edge_thesis_v1", lane="etf_momentum", owner="op",
    )


def _good_series():
    rng = random.Random(0)
    primary = [0.01 + rng.gauss(0, 0.005) for _ in range(60)]
    xs = [[0.01 + rng.gauss(0, 0.005) for _ in range(60)] if i == 0
          else [rng.gauss(0, 0.02) for _ in range(60)]
          for i in range(5)]
    return primary, xs


def test_happy_path_produces_passing_artifact(ledger_conn) -> None:
    _seed_registry(ledger_conn)
    primary, xs = _good_series()
    result = run_cycle(
        ledger_conn,
        strategy_id="ETF_MOM", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=MockPersonaRunner(
            role="quant_research_lead.v1", verdict="support",
        ),
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="support",
        ),
        policy_hash=_BUNDLE.combined_hash,
        feature_snapshot_id="fs-phase5-test",
        validation_policy_lock=_VP,
        code_hash="c", config_hash="cf",
        primary_returns=primary, cross_section_returns=xs,
        sweep_metric={1.0: 0.4, 2.0: 0.9, 3.0: 0.91,
                      4.0: 0.5, 5.0: 0.4},
        ablation_series=[("full", 1.5), ("baseline", 0.5)],
        walk_forward_folds=6, oos_period_days=300,
        trades_per_regime=40,
    )
    assert result.intake.accepted
    assert result.artifact_id is not None


def test_intake_block_short_circuits(ledger_conn) -> None:
    _seed_registry(ledger_conn)
    primary, xs = _good_series()
    result = run_cycle(
        ledger_conn,
        strategy_id="ETF_MOM", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=MockPersonaRunner(
            role="quant_research_lead.v1", verdict="support",
        ),
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="block", confidence=0.9,
        ),
        policy_hash="ph", feature_snapshot_id="fs",
        validation_policy_lock=_VP,
        code_hash="c", config_hash="cf",
        primary_returns=primary, cross_section_returns=xs,
        sweep_metric={1.0: 0.4, 2.0: 0.9}, ablation_series=[("full", 1.0)],
        walk_forward_folds=6, oos_period_days=300, trades_per_regime=40,
    )
    assert not result.intake.accepted
    assert result.report is None
    assert result.artifact_id is None


def test_failure_memory_blocks_resubmission(ledger_conn) -> None:
    _seed_registry(ledger_conn)
    primary, xs = _good_series()
    now = dt.datetime(2026, 5, 13, tzinfo=dt.timezone.utc)
    # Pre-seed a rejection for this hypothesis hash.
    record_rejection(
        ledger_conn, hypothesis_hash=_proposal().hash(),
        reason="prior tier-1 fail", now=now,
    )
    result = run_cycle(
        ledger_conn,
        strategy_id="ETF_MOM", strategy_ver=1,
        hypothesis=_proposal(),
        research_lead_runner=MockPersonaRunner(
            role="quant_research_lead.v1", verdict="support",
        ),
        risk_validator_runner=MockPersonaRunner(
            role="risk_validator.v1", verdict="support",
        ),
        policy_hash="ph", feature_snapshot_id="fs",
        validation_policy_lock=_VP,
        code_hash="c", config_hash="cf",
        primary_returns=primary, cross_section_returns=xs,
        sweep_metric={1.0: 0.4, 2.0: 0.9}, ablation_series=[("full", 1.0)],
        walk_forward_folds=6, oos_period_days=300, trades_per_regime=40,
        now=now,
    )
    assert result.blocked_by_failure_memory
    assert result.intake.accepted is False
    assert result.artifact_id is None
