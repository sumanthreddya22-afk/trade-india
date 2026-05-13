"""Phase 2 — lane caps + lane status + per-lane daily loss."""
from __future__ import annotations

from trading_bot.risk.lane_caps import (
    check_lane_status, check_per_lane_allocation,
    check_per_lane_daily_loss, demote_on_breach,
)
from trading_bot.risk.limits import LaneLimits
from trading_bot.risk.types import AccountState, Position


L = LaneLimits(per_lane_allocation_max_pct=40.0,
               per_lane_daily_loss_max_pct=0.5)

ACCT = AccountState(equity=10_000, cash=5_000,
                    equity_at_session_start=10_000, day_trade_count=0)


def test_lane_status_active_passes() -> None:
    lock = {"lanes": {"etf_momentum": {"status": "tiny_paper"}}}
    d = check_lane_status(lane="etf_momentum", lane_caps_lock=lock,
                          intent_side="buy")
    assert d.verdict == "accept"


def test_lane_status_research_only_blocks_entry() -> None:
    lock = {"lanes": {"mean_reversion": {"status": "research_only"}}}
    d = check_lane_status(lane="mean_reversion", lane_caps_lock=lock,
                          intent_side="buy")
    assert d.verdict == "halt"


def test_lane_status_reduce_only_blocks_entry_allows_exit() -> None:
    lock = {"lanes": {"crypto_trend": {"status": "reduce_only"}}}
    assert check_lane_status(
        lane="crypto_trend", lane_caps_lock=lock, intent_side="buy",
    ).verdict == "halt"
    assert check_lane_status(
        lane="crypto_trend", lane_caps_lock=lock,
        intent_side="sell_to_close",
    ).verdict == "accept"


def test_lane_status_unknown_lane_halts() -> None:
    lock = {"lanes": {}}
    d = check_lane_status(lane="ghost", lane_caps_lock=lock,
                          intent_side="buy")
    assert d.verdict == "halt"


def test_lane_allocation_under_cap_passes() -> None:
    d = check_per_lane_allocation(
        lane="etf_momentum", intent_notional=1500, intent_side="buy",
        account=ACCT,
        positions=[Position(symbol="SPY", asset_class="equity", qty=1,
                            market_value=2000, classification="bot",
                            lane="etf_momentum")],
        limits=L,
    )
    assert d.verdict == "accept"


def test_lane_allocation_over_cap_halts() -> None:
    d = check_per_lane_allocation(
        lane="etf_momentum", intent_notional=500, intent_side="buy",
        account=ACCT,
        positions=[Position(symbol="SPY", asset_class="equity", qty=1,
                            market_value=3800, classification="bot",
                            lane="etf_momentum")],
        limits=L,
    )
    assert d.verdict == "halt"
    assert "allocation" in d.reason


def test_lane_daily_loss_pass() -> None:
    assert check_per_lane_daily_loss(
        lane="etf_momentum", lane_session_pnl_pct=-0.3, limits=L,
    ).verdict == "accept"


def test_lane_daily_loss_breach() -> None:
    d = check_per_lane_daily_loss(
        lane="etf_momentum", lane_session_pnl_pct=-0.51, limits=L,
    )
    assert d.verdict == "halt"


def test_demote_on_breach_mutates_lock() -> None:
    lock = {"lanes": {"etf_momentum": {"status": "tiny_paper"}}}
    new_status = demote_on_breach(
        lane="etf_momentum", breach_reason="daily_loss",
        lane_caps_lock_mutable=lock,
    )
    assert new_status == "observe_only"
    assert lock["lanes"]["etf_momentum"]["status"] == "observe_only"


def test_demote_noop_on_inactive_lane() -> None:
    lock = {"lanes": {"x": {"status": "research_only"}}}
    assert demote_on_breach(
        lane="x", breach_reason="-", lane_caps_lock_mutable=lock,
    ) is None
