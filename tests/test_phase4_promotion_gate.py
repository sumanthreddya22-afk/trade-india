"""Phase 4 — promotion gate (Tier-1 / 2 / 3 + human sign-off)."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.registry import (
    GATE_LENS, TIER_LIVE, TIER_PAPER, TIER_RESEARCH,
    gate, record_promotion_packet, record_validation_artifact,
    register_version,
)
from trading_bot.risk import load_policy


_BUNDLE = load_policy()
_VP = _BUNDLE.validation_policy


def _seed_research_pass(conn, strategy_id="X", ver=1) -> str:
    artifact_id, _ = record_validation_artifact(
        conn,
        strategy_id=strategy_id, strategy_ver=ver,
        tier=TIER_RESEARCH, code_hash="c", config_hash="cf",
        metrics={"oos_dsr": 0.55, "pbo": 0.40,
                 "walk_forward_folds": 6,
                 "oos_period_days": 300,
                 "trades_per_regime": 40, "lens": GATE_LENS},
        validation_policy_lock=_VP,
    )
    return artifact_id


def _seed_paper_pass(conn, strategy_id="X", ver=1) -> str:
    artifact_id, _ = record_validation_artifact(
        conn,
        strategy_id=strategy_id, strategy_ver=ver, tier=TIER_PAPER,
        code_hash="c", config_hash="cf",
        metrics={"oos_dsr": 0.72, "pbo": 0.30,
                 "walk_forward_folds": 6, "oos_period_days": 600,
                 "trades_per_regime": 60, "paper_obs_days": 70,
                 "paper_trade_count_or_rebalances": 30,
                 "max_drawdown_paper_pct": 0.05, "lens": GATE_LENS},
        validation_policy_lock=_VP,
    )
    return artifact_id


def _seed_live_pass(conn, strategy_id="X", ver=1) -> str:
    artifact_id, _ = record_validation_artifact(
        conn,
        strategy_id=strategy_id, strategy_ver=ver, tier=TIER_LIVE,
        code_hash="c", config_hash="cf",
        metrics={"oos_dsr": 0.90, "pbo": 0.20,
                 "max_drawdown_paper_pct": 0.04,
                 "sharpe_tstat_net": 2.5,
                 "excess_over_benchmark_annual_pct": 2.5,
                 "paper_rebalance_events": 14, "lens": GATE_LENS},
        validation_policy_lock=_VP,
    )
    return artifact_id


# ---------------------------------------------------------------------------


def test_research_only_to_shadow_requires_tier1(ledger_conn) -> None:
    register_version(ledger_conn, strategy_id="X", strategy_ver=1,
                     code_hash="c", config_hash="cf",
                     thesis_id="t", hypothesis_id="h",
                     lane="etf_momentum", owner="op")
    decision = gate(
        ledger_conn, strategy_id="X", strategy_ver=1,
        target_status="shadow", validation_policy_lock=_VP,
    )
    assert not decision.allowed
    assert "no passing Tier-research_candidate" in decision.reason


def test_promotion_to_shadow_with_tier1_pass(ledger_conn) -> None:
    register_version(ledger_conn, strategy_id="X", strategy_ver=1,
                     code_hash="c", config_hash="cf",
                     thesis_id="t", hypothesis_id="h",
                     lane="etf_momentum", owner="op")
    _seed_research_pass(ledger_conn, "X", 1)
    decision = gate(
        ledger_conn, strategy_id="X", strategy_ver=1,
        target_status="shadow", validation_policy_lock=_VP,
    )
    assert decision.allowed
    assert decision.tier_required == TIER_RESEARCH


def test_promotion_to_tiny_paper_requires_tier2(ledger_conn) -> None:
    register_version(ledger_conn, strategy_id="X", strategy_ver=1,
                     code_hash="c", config_hash="cf",
                     thesis_id="t", hypothesis_id="h",
                     lane="etf_momentum", owner="op")
    _seed_research_pass(ledger_conn, "X", 1)
    decision = gate(
        ledger_conn, strategy_id="X", strategy_ver=1,
        target_status="tiny_paper", validation_policy_lock=_VP,
    )
    assert not decision.allowed
    assert "Tier-paper_candidate" in decision.reason

    _seed_paper_pass(ledger_conn, "X", 1)
    decision = gate(
        ledger_conn, strategy_id="X", strategy_ver=1,
        target_status="tiny_paper", validation_policy_lock=_VP,
    )
    assert decision.allowed


def test_artifact_version_must_match(ledger_conn) -> None:
    register_version(ledger_conn, strategy_id="X", strategy_ver=2,
                     code_hash="c", config_hash="cf",
                     thesis_id="t", hypothesis_id="h",
                     lane="etf_momentum", owner="op")
    _seed_research_pass(ledger_conn, "X", 1)
    decision = gate(
        ledger_conn, strategy_id="X", strategy_ver=2,
        target_status="shadow", validation_policy_lock=_VP,
    )
    assert not decision.allowed
    assert "does not match requested ver" in decision.reason


def test_promotion_to_live_requires_signed_packet(ledger_conn) -> None:
    register_version(ledger_conn, strategy_id="X", strategy_ver=1,
                     code_hash="c", config_hash="cf",
                     thesis_id="t", hypothesis_id="h",
                     lane="etf_momentum", owner="op")
    artifact_id = _seed_live_pass(ledger_conn, "X", 1)

    # Without packet -> blocked, signoff required.
    d = gate(ledger_conn, strategy_id="X", strategy_ver=1,
             target_status="live", validation_policy_lock=_VP)
    assert not d.allowed
    assert d.human_signoff_required

    # Unsigned packet -> blocked.
    packet_id = record_promotion_packet(
        ledger_conn,
        strategy_id="X", strategy_ver=1, target_tier=TIER_LIVE,
        code_hash="c", config_hash="cf",
        validation_artifact_id=artifact_id, operator_signed=False,
    )
    d = gate(ledger_conn, strategy_id="X", strategy_ver=1,
             target_status="live", validation_policy_lock=_VP,
             promotion_packet_id=packet_id)
    assert not d.allowed
    assert "not operator-signed" in d.reason

    # Signed packet -> allowed.
    packet_id = record_promotion_packet(
        ledger_conn,
        strategy_id="X", strategy_ver=1, target_tier=TIER_LIVE,
        code_hash="c", config_hash="cf",
        validation_artifact_id=artifact_id, operator_signed=True,
    )
    d = gate(ledger_conn, strategy_id="X", strategy_ver=1,
             target_status="live", validation_policy_lock=_VP,
             promotion_packet_id=packet_id)
    assert d.allowed


def test_promotion_packet_wrong_artifact_rejected(ledger_conn) -> None:
    register_version(ledger_conn, strategy_id="X", strategy_ver=1,
                     code_hash="c", config_hash="cf",
                     thesis_id="t", hypothesis_id="h",
                     lane="etf_momentum", owner="op")
    _seed_live_pass(ledger_conn, "X", 1)
    packet_id = record_promotion_packet(
        ledger_conn,
        strategy_id="X", strategy_ver=1, target_tier=TIER_LIVE,
        code_hash="c", config_hash="cf",
        validation_artifact_id="WRONG_ARTIFACT",
        operator_signed=True,
    )
    d = gate(ledger_conn, strategy_id="X", strategy_ver=1,
             target_status="live", validation_policy_lock=_VP,
             promotion_packet_id=packet_id)
    assert not d.allowed
    assert "different artifact" in d.reason


def test_invalid_target_status_rejected(ledger_conn) -> None:
    decision = gate(
        ledger_conn, strategy_id="X", strategy_ver=1,
        target_status="observe_only", validation_policy_lock=_VP,
    )
    assert not decision.allowed
    assert "not a promotable status" in decision.reason
