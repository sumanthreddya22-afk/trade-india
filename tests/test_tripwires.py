"""WS5d — 4 P&L tripwires."""
from __future__ import annotations

from trading_bot.risk.tripwires import (
    evaluate_behavioural, evaluate_drift, evaluate_exec_quality,
    evaluate_realized_loss,
)


def test_realized_loss_no_breach() -> None:
    assert evaluate_realized_loss(realized_loss_usd=0, equity_usd=1000) is None
    assert evaluate_realized_loss(realized_loss_usd=-10, equity_usd=1000) is None


def test_realized_loss_alert_via_pct() -> None:
    f = evaluate_realized_loss(realized_loss_usd=-21, equity_usd=1000)
    assert f is not None and f.severity == "alert"


def test_realized_loss_halt_via_pct() -> None:
    f = evaluate_realized_loss(realized_loss_usd=-31, equity_usd=1000)
    assert f is not None and f.severity == "halt"


def test_realized_loss_alert_via_usd_floor_on_huge_account() -> None:
    # Pct 0.1%, but $20 absolute floor still fires.
    f = evaluate_realized_loss(realized_loss_usd=-20.0, equity_usd=1_000_000)
    assert f is not None and f.severity == "alert"


def test_drift_alert_and_halt() -> None:
    assert evaluate_drift(realised_mean_bps=5.0, modelled_mean_bps=5.0) is None
    f = evaluate_drift(realised_mean_bps=6.5, modelled_mean_bps=5.0)
    assert f is not None and f.severity == "alert"
    f = evaluate_drift(realised_mean_bps=8.0, modelled_mean_bps=5.0)
    assert f is not None and f.severity == "halt"


def test_exec_quality_uses_absolute_slippage() -> None:
    assert evaluate_exec_quality(recent_slippages_bps=[-2, 3, 1]) is None
    f = evaluate_exec_quality(recent_slippages_bps=[12, -11, 10, -10, 11])
    assert f is not None and f.severity == "alert"
    f = evaluate_exec_quality(recent_slippages_bps=[17, 18, 16, 15, 20])
    assert f is not None and f.severity == "halt"


def test_behavioural_zero_trades_alerts() -> None:
    f = evaluate_behavioural(
        observed_trade_count=0, expected_trade_count=5,
        observed_position_count=3, expected_position_count=3,
    )
    assert f is not None and f.severity == "alert"


def test_behavioural_extreme_trade_count_halts() -> None:
    f = evaluate_behavioural(
        observed_trade_count=20, expected_trade_count=5,
        observed_position_count=3, expected_position_count=3,
    )
    assert f is not None and f.severity == "halt"


def test_behavioural_excess_positions_halt() -> None:
    f = evaluate_behavioural(
        observed_trade_count=5, expected_trade_count=5,
        observed_position_count=10, expected_position_count=5,
    )
    assert f is not None and f.severity == "halt"
