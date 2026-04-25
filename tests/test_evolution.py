from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_bot.evolution import (
    Proposal,
    apply_proposals,
    append_evolution_log,
    evaluate_performance,
    load_params,
    propose_rule_changes,
    save_params,
)
from trading_bot.reconciliation import ClosedTrade


def _t(eid: str, pnl_pct: float, strategy: str = "momentum", pnl_dollar: float | None = None) -> ClosedTrade:
    return ClosedTrade(
        symbol="AAPL", side="buy", qty=Decimal("3"),
        entry_price=Decimal("100"), exit_price=Decimal(str(100 * (1 + pnl_pct / 100))),
        realized_pnl=Decimal(str(pnl_dollar if pnl_dollar is not None else 3 * pnl_pct)),
        pnl_pct=pnl_pct,
        strategy=strategy, regime="trending_up",
        entry_time=datetime(2026, 4, 1, tzinfo=timezone.utc),
        exit_time=datetime(2026, 4, 2, tzinfo=timezone.utc),
        hold_hours=24.0, entry_order_id=eid,
    )


def test_evaluate_performance_basic_stats():
    trades = [_t(f"e{i}", pnl_pct=2.0) for i in range(5)] + [_t(f"l{i}", pnl_pct=-1.0) for i in range(5)]
    stats = evaluate_performance(trades, min_trades=5)
    s = stats["momentum"]
    assert s.n_trades == 10
    assert s.win_rate == 0.5
    assert s.avg_win_pct == 2.0
    assert s.avg_loss_pct == -1.0
    assert s.profit_factor == pytest.approx(2.0, rel=0.01)


def test_evaluate_performance_skips_under_min():
    trades = [_t(f"e{i}", pnl_pct=2.0) for i in range(3)]
    stats = evaluate_performance(trades, min_trades=5)
    assert stats == {}


def test_propose_changes_for_low_winrate():
    trades = [_t(f"e{i}", pnl_pct=1.0) for i in range(8)] + [_t(f"l{i}", pnl_pct=-2.0) for i in range(15)]
    stats = evaluate_performance(trades, min_trades=5)
    params = {"momentum": {"per_trade_risk_pct": 0.5, "stop_pct": 0.05}}
    proposals = propose_rule_changes(stats, params)
    # Should propose risk reduction (win rate 35% < 40%)
    risk_props = [p for p in proposals if "per_trade_risk_pct" in p.parameter]
    assert len(risk_props) >= 1
    assert risk_props[0].suggested_value < risk_props[0].current_value


def test_propose_changes_for_high_winrate_with_pf():
    # 80% wins with PF > 1.5 → suggest scaling up
    wins = [_t(f"w{i}", pnl_pct=2.0, pnl_dollar=20) for i in range(16)]
    losses = [_t(f"l{i}", pnl_pct=-1.0, pnl_dollar=-5) for i in range(4)]
    stats = evaluate_performance(wins + losses, min_trades=5)
    params = {"momentum": {"per_trade_risk_pct": 0.5, "stop_pct": 0.05}}
    proposals = propose_rule_changes(stats, params)
    risk_props = [p for p in proposals if "per_trade_risk_pct" in p.parameter]
    assert len(risk_props) >= 1
    assert risk_props[0].suggested_value > risk_props[0].current_value


def test_apply_proposals_mutates_params():
    params = {"momentum": {"per_trade_risk_pct": 0.5}}
    proposals = [Proposal(
        description="x", rationale="y", parameter="momentum.per_trade_risk_pct",
        current_value=0.5, suggested_value=0.25, confidence="medium",
    )]
    new_params = apply_proposals(params, proposals)
    assert new_params["momentum"]["per_trade_risk_pct"] == 0.25
    # original unchanged
    assert params["momentum"]["per_trade_risk_pct"] == 0.5


def test_save_and_load_params(tmp_path: Path):
    p = tmp_path / "params.yaml"
    save_params(p, {"momentum": {"rsi_lower": 60.0}})
    loaded = load_params(p)
    # default fills missing keys
    assert loaded["momentum"]["rsi_lower"] == 60.0
    assert "rsi_upper" in loaded["momentum"]


def test_append_evolution_log(tmp_path: Path):
    rules = tmp_path / "rules.md"
    rules.write_text("# Strategy Rules\n\n## Evolution Log\n")
    stats = evaluate_performance(
        [_t(f"w{i}", pnl_pct=1.5) for i in range(6)] + [_t(f"l{i}", pnl_pct=-1.0) for i in range(2)],
        min_trades=5,
    )
    proposals = [Proposal(
        description="test prop", rationale="reason",
        parameter="momentum.stop_pct", current_value=0.05, suggested_value=0.04, confidence="medium",
    )]
    append_evolution_log(rules, stats, proposals, applied=False)
    content = rules.read_text()
    assert "performance review" in content
    assert "test prop" in content
    assert "0.05" in content and "0.04" in content
