"""Per-strategy 30-day loss cap.

Plan v4 §6: when a strategy's realised + unrealised loss over the last
30 days exceeds 1.5% of equity, it is moved to observe-only and a
mutation request is opened.
"""
from __future__ import annotations

from trading_bot.risk.limits import StrategyLimits
from trading_bot.risk.types import RiskDecision


def check_per_strategy_loss(
    *,
    strategy_id: str,
    strategy_30d_loss_pct: float,
    limits: StrategyLimits,
) -> RiskDecision:
    """``strategy_30d_loss_pct`` is positive for losses (-3% loss arrives
    here as +3.0). The caller computes the value from closed PnL +
    unrealised PnL on currently-open positions tagged with this
    strategy.
    """
    if strategy_30d_loss_pct >= limits.realized_plus_unrealized_loss_30d_max_pct:
        return RiskDecision.halt(
            f"strategy_cap:{strategy_id}:30d_loss "
            f"({strategy_30d_loss_pct:.2f}% >= "
            f"{limits.realized_plus_unrealized_loss_30d_max_pct:.2f}%)"
        )
    return RiskDecision.accept()


__all__ = ["check_per_strategy_loss"]
