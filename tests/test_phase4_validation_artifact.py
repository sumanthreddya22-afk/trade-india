"""Phase 4 — validation_artifact: tier evaluation + record + hash chain."""
from __future__ import annotations

from trading_bot.ledger import verify_chain
from trading_bot.registry import (
    GATE_LENS, TIER_LIVE, TIER_PAPER, TIER_RESEARCH,
    evaluate_tier, find_latest_pass, record_validation_artifact,
)
from trading_bot.risk import load_policy


_BUNDLE = load_policy()
_VP = _BUNDLE.validation_policy


# ---------------------------------------------------------------------------
# Tier evaluation
# ---------------------------------------------------------------------------


def _good_research_metrics():
    return {
        "oos_dsr": 0.55, "pbo": 0.40, "walk_forward_folds": 6,
        "oos_period_days": 300, "trades_per_regime": 40,
        "lens": GATE_LENS,
    }


def _good_paper_metrics():
    return {
        "oos_dsr": 0.72, "pbo": 0.30, "walk_forward_folds": 6,
        "oos_period_days": 600, "trades_per_regime": 60,
        "paper_obs_days": 70, "paper_trade_count_or_rebalances": 30,
        "max_drawdown_paper_pct": 0.05, "lens": GATE_LENS,
    }


def _good_live_metrics():
    return {
        "oos_dsr": 0.90, "pbo": 0.20,
        "max_drawdown_paper_pct": 0.04,
        "sharpe_tstat_net": 2.5,
        "excess_over_benchmark_annual_pct": 2.5,
        "paper_rebalance_events": 14, "lens": GATE_LENS,
    }


def test_research_pass() -> None:
    e = evaluate_tier(tier=TIER_RESEARCH,
                      metrics=_good_research_metrics(),
                      validation_policy_lock=_VP)
    assert e.pass_
    assert e.failure_reasons == ()


def test_research_fail_low_dsr() -> None:
    m = _good_research_metrics()
    m["oos_dsr"] = 0.20
    e = evaluate_tier(tier=TIER_RESEARCH, metrics=m,
                      validation_policy_lock=_VP)
    assert not e.pass_
    assert any("DSR" in r for r in e.failure_reasons)


def test_research_fail_high_pbo() -> None:
    m = _good_research_metrics()
    m["pbo"] = 0.99
    e = evaluate_tier(tier=TIER_RESEARCH, metrics=m,
                      validation_policy_lock=_VP)
    assert not e.pass_
    assert any("PBO" in r for r in e.failure_reasons)


def test_paper_pass() -> None:
    e = evaluate_tier(tier=TIER_PAPER, metrics=_good_paper_metrics(),
                      validation_policy_lock=_VP)
    assert e.pass_


def test_live_pass() -> None:
    e = evaluate_tier(tier=TIER_LIVE, metrics=_good_live_metrics(),
                      validation_policy_lock=_VP)
    assert e.pass_


def test_live_fail_low_tstat() -> None:
    m = _good_live_metrics()
    m["sharpe_tstat_net"] = 1.5
    e = evaluate_tier(tier=TIER_LIVE, metrics=m,
                      validation_policy_lock=_VP)
    assert not e.pass_
    assert any("t-stat" in r for r in e.failure_reasons)


def test_wrong_lens_fails() -> None:
    m = _good_research_metrics()
    m["lens"] = "broker_paper"
    e = evaluate_tier(tier=TIER_RESEARCH, metrics=m,
                      validation_policy_lock=_VP)
    assert not e.pass_
    assert any("lens" in r for r in e.failure_reasons)


# ---------------------------------------------------------------------------
# Recording + hash chain
# ---------------------------------------------------------------------------


def test_record_writes_row_and_evaluates(ledger_conn) -> None:
    artifact_id, evaluation = record_validation_artifact(
        ledger_conn,
        strategy_id="X", strategy_ver=1, tier=TIER_RESEARCH,
        code_hash="c", config_hash="cf",
        metrics=_good_research_metrics(),
        validation_policy_lock=_VP,
    )
    assert evaluation.pass_
    cur = ledger_conn.cursor()
    cur.execute("SELECT pass FROM validation_artifact WHERE artifact_id=?",
                (artifact_id,))
    assert cur.fetchone()[0] == 1


def test_record_writes_failing_row_with_reasons(ledger_conn) -> None:
    m = _good_research_metrics()
    m["pbo"] = 0.99
    artifact_id, evaluation = record_validation_artifact(
        ledger_conn,
        strategy_id="X", strategy_ver=1, tier=TIER_RESEARCH,
        code_hash="c", config_hash="cf",
        metrics=m, validation_policy_lock=_VP,
    )
    assert not evaluation.pass_
    cur = ledger_conn.cursor()
    cur.execute("SELECT pass, failure_reasons FROM validation_artifact "
                "WHERE artifact_id=?", (artifact_id,))
    p, reasons = cur.fetchone()
    assert p == 0
    assert "PBO" in reasons


def test_record_chains_hashes(ledger_conn) -> None:
    record_validation_artifact(
        ledger_conn, strategy_id="A", strategy_ver=1, tier=TIER_RESEARCH,
        code_hash="a", config_hash="ac",
        metrics=_good_research_metrics(),
        validation_policy_lock=_VP,
    )
    record_validation_artifact(
        ledger_conn, strategy_id="B", strategy_ver=1, tier=TIER_RESEARCH,
        code_hash="b", config_hash="bc",
        metrics=_good_research_metrics(),
        validation_policy_lock=_VP,
    )
    assert verify_chain(ledger_conn, "validation_artifact") == 2


def test_find_latest_pass(ledger_conn) -> None:
    # First record a fail, then a pass; find_latest_pass returns the pass.
    bad = _good_research_metrics()
    bad["pbo"] = 0.99
    record_validation_artifact(
        ledger_conn, strategy_id="Y", strategy_ver=1, tier=TIER_RESEARCH,
        code_hash="c", config_hash="cf", metrics=bad,
        validation_policy_lock=_VP,
    )
    record_validation_artifact(
        ledger_conn, strategy_id="Y", strategy_ver=1, tier=TIER_RESEARCH,
        code_hash="c", config_hash="cf",
        metrics=_good_research_metrics(),
        validation_policy_lock=_VP,
    )
    result = find_latest_pass(ledger_conn, strategy_id="Y",
                              tier=TIER_RESEARCH)
    assert result is not None
    assert result["strategy_ver"] == 1
