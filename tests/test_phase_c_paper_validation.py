"""Phase C — paper-submit validation + auto-register."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest

from trading_bot.registry.auto_register import auto_register_v_n_plus_1
from trading_bot.registry.strategies import register_version
from trading_bot.research.paper_validation import (
    PaperValidationReport, validate_via_paper_submit,
)


@dataclass
class _FakeDecision:
    intents: list[dict]


def test_paper_validation_dry_run_passes() -> None:
    def eval_fn(*, params: dict) -> _FakeDecision:
        return _FakeDecision(intents=[
            {"symbol": "SPY", "qty": 10.0, "intent_price": 500.0,
             "side": "buy", "asset_class": "us_equity"},
        ])
    report = validate_via_paper_submit(
        candidate_id="cand-1",
        candidate_params={"lookback_days": 200},
        strategy_family="ETF_MOMENTUM_v3",
        evaluate_fn=eval_fn,
        num_test_decisions=3,
    )
    assert report.passed is True
    assert report.submitted == 3
    assert report.filled == 3


def test_paper_validation_with_risk_rejection() -> None:
    def eval_fn(*, params: dict) -> _FakeDecision:
        return _FakeDecision(intents=[
            {"symbol": "SPY", "qty": 10.0, "intent_price": 500.0,
             "side": "buy"},
        ])

    def risk_reject(_intent: dict) -> bool:
        return False  # always reject

    report = validate_via_paper_submit(
        candidate_id="cand-2",
        candidate_params={},
        strategy_family="X",
        evaluate_fn=eval_fn,
        risk_precheck_fn=risk_reject,
        num_test_decisions=2,
    )
    assert report.passed is False
    assert report.risk_rejected >= 2


def test_paper_validation_with_broker_slippage() -> None:
    def eval_fn(*, params: dict) -> _FakeDecision:
        return _FakeDecision(intents=[
            {"symbol": "SPY", "qty": 10.0, "intent_price": 500.0,
             "side": "buy"},
        ])

    def broker(intent: dict) -> dict:
        return {
            "filled": True,
            "intent_price": intent["intent_price"],
            "fill_price": intent["intent_price"] * 1.001,  # 10 bps slip
        }

    report = validate_via_paper_submit(
        candidate_id="cand-3",
        candidate_params={},
        strategy_family="X",
        evaluate_fn=eval_fn,
        broker_submit_fn=broker,
        num_test_decisions=2,
        pessimistic_slippage_bps_tolerance=20.0,
    )
    assert report.passed is True
    assert 5 < report.avg_slippage_bps < 15


def test_auto_register_voids_when_live_capital_enabled(
    ledger_conn, tmp_path: Path,
) -> None:
    import json
    (tmp_path / "live_capital.lock").write_text(json.dumps({
        "live_capital_enabled": True,
    }))
    (tmp_path / "paper_fast_track_v1.lock").write_text(json.dumps({
        "enabled": True,
    }))
    result = auto_register_v_n_plus_1(
        ledger_conn,
        strategy_id_base="X",
        candidate_params={}, candidate_id="c",
        code_hash="c", config_hash="cf",
        thesis_id="t", hypothesis_id="h",
        lane="etf_momentum",
        policy_dir=tmp_path,
    )
    assert result.registered is False
    assert "live_capital" in result.reason.lower()


def test_auto_register_inserts_version(ledger_conn, tmp_path: Path) -> None:
    import json
    (tmp_path / "live_capital.lock").write_text(json.dumps({
        "live_capital_enabled": False,
    }))
    (tmp_path / "paper_fast_track_v1.lock").write_text(json.dumps({
        "enabled": True,
    }))
    # Need a v=1 row first so we can compute the next version.
    register_version(
        ledger_conn, strategy_id="X", strategy_ver=1,
        code_hash="c", config_hash="cf",
        thesis_id="t", hypothesis_id="h",
        lane="etf_momentum", owner="op",
    )
    result = auto_register_v_n_plus_1(
        ledger_conn,
        strategy_id_base="X",
        candidate_params={}, candidate_id="c",
        code_hash="c2", config_hash="cf2",
        thesis_id="t", hypothesis_id="h",
        lane="etf_momentum",
        policy_dir=tmp_path,
    )
    assert result.registered is True
    assert result.new_strategy_ver == 2
