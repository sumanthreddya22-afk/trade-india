"""Account-level cap checks: daily DD, trailing DD, intraday PnL floor.

Each function returns a ``RiskDecision`` — ``accept`` if within budget,
``halt`` if breached. The trailing-DD check requires a 60-day equity
series; Phase 2 ships the math, the runtime series feed lands once
``position_snapshot`` rows accumulate.
"""
from __future__ import annotations

from typing import Sequence

from trading_bot.risk.limits import AccountLimits
from trading_bot.risk.types import AccountState, RiskDecision


def check_daily_drawdown(
    account: AccountState, limits: AccountLimits,
) -> RiskDecision:
    """Halt new entries when realised + unrealised intraday loss exceeds
    the daily DD threshold (default 1.0% of session-start equity).
    """
    if account.equity_at_session_start <= 0:
        # No baseline — treat as session start; can't compute DD yet.
        return RiskDecision.accept()
    loss_pct = (account.equity_at_session_start - account.equity) \
        / account.equity_at_session_start * 100.0
    if loss_pct >= limits.daily_drawdown_pct:
        return RiskDecision.halt(
            f"account_cap:daily_drawdown ({loss_pct:.2f}% >= "
            f"{limits.daily_drawdown_pct:.2f}%)"
        )
    return RiskDecision.accept()


def check_intraday_pnl_floor(
    account: AccountState, limits: AccountLimits,
) -> RiskDecision:
    """Halt when daily PnL drops below the intraday floor (default -1.5%).

    This is a kill-switch-grade check; it duplicates daily DD slightly
    but uses a stricter threshold to catch tail moves quickly.
    """
    if account.equity_at_session_start <= 0:
        return RiskDecision.accept()
    pnl_pct = (account.equity - account.equity_at_session_start) \
        / account.equity_at_session_start * 100.0
    if pnl_pct <= limits.intraday_pnl_floor_pct:
        return RiskDecision.halt(
            f"kill_switch:intraday_pnl_floor "
            f"({pnl_pct:.2f}% <= {limits.intraday_pnl_floor_pct:.2f}%)"
        )
    return RiskDecision.accept()


def check_trailing_drawdown(
    equity_history: Sequence[float],
    limits: AccountLimits,
) -> RiskDecision:
    """Trailing peak-to-trough drawdown over the rolling window.

    Returns ``halt`` once the rolling DD exceeds the threshold (5%).
    ``equity_history`` is a sequence of end-of-session equity values
    ordered oldest-first, capped at ``trailing_drawdown_window_days``.
    """
    if len(equity_history) < 2:
        return RiskDecision.accept()
    series = list(equity_history)[-limits.trailing_drawdown_window_days:]
    peak = series[0]
    max_dd_pct = 0.0
    for v in series:
        if v > peak:
            peak = v
        if peak > 0:
            dd_pct = (peak - v) / peak * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
    if max_dd_pct >= limits.trailing_drawdown_pct:
        return RiskDecision.halt(
            f"account_cap:trailing_drawdown "
            f"({max_dd_pct:.2f}% >= {limits.trailing_drawdown_pct:.2f}%)"
        )
    return RiskDecision.accept()


__all__ = [
    "check_daily_drawdown",
    "check_intraday_pnl_floor",
    "check_trailing_drawdown",
]
