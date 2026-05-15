"""Phase 2 — per-symbol cap (reduce-to-fit) + per-order at-risk cap."""
from __future__ import annotations

from trading_bot.risk.limits import OrderLimits, SymbolLimits
from trading_bot.risk.symbol_order_caps import (
    check_per_order_cap, check_per_symbol_cap,
)
from trading_bot.risk.types import AccountState, Position


SYM_L = SymbolLimits(per_symbol_gross_max_pct=5.0)
ORD_L = OrderLimits(per_order_at_risk_max_pct=2.0,
                    stop_coverage_required_within_seconds=60)

ACCT = AccountState(equity=10_000, cash=5_000,
                    equity_at_session_start=10_000, day_trade_count=0)


def test_symbol_under_cap_accepts() -> None:
    d = check_per_symbol_cap(
        intent_symbol="SPY", intent_qty=2, intent_price=100,
        intent_side="buy", account=ACCT,
        positions=[Position(symbol="SPY", asset_class="equity", qty=1,
                            market_value=200, classification="bot")],
        limits=SYM_L,
    )
    assert d.verdict == "accept"


def test_symbol_over_cap_reduces() -> None:
    # 5% of 10k = 500. Already 300 in SPY; trying to add 5 @ 100 = 500;
    # headroom = 200; reduced qty = 200/100 = 2.0.
    d = check_per_symbol_cap(
        intent_symbol="SPY", intent_qty=5, intent_price=100,
        intent_side="buy", account=ACCT,
        positions=[Position(symbol="SPY", asset_class="equity", qty=3,
                            market_value=300, classification="bot")],
        limits=SYM_L,
    )
    assert d.verdict == "reduce"
    assert abs(d.adjusted_qty - 2.0) < 1e-6


def test_symbol_already_at_cap_halts() -> None:
    d = check_per_symbol_cap(
        intent_symbol="SPY", intent_qty=1, intent_price=100,
        intent_side="buy", account=ACCT,
        positions=[Position(symbol="SPY", asset_class="equity", qty=5,
                            market_value=500, classification="bot")],
        limits=SYM_L,
    )
    assert d.verdict == "halt"


def test_symbol_exit_skipped() -> None:
    d = check_per_symbol_cap(
        intent_symbol="SPY", intent_qty=1, intent_price=100,
        intent_side="sell_to_close", account=ACCT, positions=[],
        limits=SYM_L,
    )
    assert d.verdict == "accept"
    assert "exit" in d.reason


def test_plain_sell_with_long_position_is_exit() -> None:
    """A plain ``sell`` against an existing long position is a reduce —
    skip the per-symbol cap so the strategy can rebalance down."""
    d = check_per_symbol_cap(
        intent_symbol="SPY", intent_qty=1, intent_price=100,
        intent_side="sell", account=ACCT,
        positions=[Position(symbol="SPY", asset_class="equity", qty=10,
                            market_value=1000, classification="bot")],
        limits=SYM_L,
    )
    assert d.verdict == "accept"
    assert "exit" in d.reason


def test_plain_sell_without_position_is_short_entry_capped() -> None:
    """A plain ``sell`` with no prior long is a sell-to-open (short equity
    or short-option entry). The per-symbol cap must apply — regression for
    the wheel's short-put entry bypassing the 5% cap."""
    # 5% of 10k = $500 cap. Order = 6 contracts × $100 strike-equiv = $600 notional.
    d = check_per_symbol_cap(
        intent_symbol="SPY_PUT_600", intent_qty=6, intent_price=100,
        intent_side="sell", account=ACCT, positions=[],
        limits=SYM_L,
    )
    # Must NOT be a free accept; cap kicks in. The order is reduced to
    # fit, not silently skipped.
    assert d.verdict == "reduce"
    assert abs(d.adjusted_qty - 5.0) < 1e-6


def test_order_at_risk_with_stop() -> None:
    # 2% of 10k = $200. qty=10 @ 100 stop=97 → at_risk = 10*3 = 30. Pass.
    d = check_per_order_cap(
        intent_qty=10, intent_price=100, intent_side="buy",
        stop_loss_price=97, account=ACCT, limits=ORD_L,
    )
    assert d.verdict == "accept"


def test_order_at_risk_too_wide_stop_halts() -> None:
    # qty=10 @ 100 stop=70 → at_risk = 10*30 = 300 > $200 cap.
    d = check_per_order_cap(
        intent_qty=10, intent_price=100, intent_side="buy",
        stop_loss_price=70, account=ACCT, limits=ORD_L,
    )
    assert d.verdict == "halt"


def test_order_at_risk_no_stop_uses_full_notional() -> None:
    # qty=3 @ 100 = 300 notional > $200 cap → halt.
    d = check_per_order_cap(
        intent_qty=3, intent_price=100, intent_side="buy",
        stop_loss_price=None, account=ACCT, limits=ORD_L,
    )
    assert d.verdict == "halt"


def test_order_exit_skipped() -> None:
    d = check_per_order_cap(
        intent_qty=10, intent_price=100, intent_side="sell_to_close",
        stop_loss_price=None, account=ACCT, limits=ORD_L,
    )
    assert d.verdict == "accept"
