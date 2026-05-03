"""Compute live RiskState from Alpaca portfolio history.

Replaces the stubbed `_build_risk_state()` in the orchestrator. Pulls the
equity timeline from Alpaca and computes daily/weekly P&L as percentages
relative to the start of the period.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest

from trading_bot.shared.config import AppConfig, Settings
from trading_bot.exceptions import AlpacaClientError
from trading_bot.shared.risk_manager import RiskState
from trading_bot.state_pause import read_halted_strategies, HALTED_STRATEGIES_PATH


@dataclass(frozen=True)
class PnlReading:
    daily_pnl_pct: Decimal
    weekly_pnl_pct: Decimal
    consecutive_losing_days: int
    halted: bool
    halt_reason: str = ""
    # Bucket A: sizing throttle. 1.0 = full size, 0.5 = half, 0.25 = quarter.
    # Computed from consecutive_losing_days vs cfg.risk.max_consecutive_losing_days.
    size_multiplier: Decimal = Decimal("1")
    # Bucket A: per-strategy halts (e.g. {"wheel"}). Sourced from a state file
    # so the operator can pause one lane without halting the whole bot.
    halted_strategies: frozenset[str] = frozenset()


class PnlStateBuilder:
    """Builds RiskState from real Alpaca portfolio history."""

    def __init__(self, settings: Settings, config: AppConfig) -> None:
        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )
        self._cfg = config

    def read(self) -> PnlReading:
        halted_strategies = read_halted_strategies(HALTED_STRATEGIES_PATH)
        try:
            req = GetPortfolioHistoryRequest(period="1W", timeframe="1D")
            hist = self._client.get_portfolio_history(req)
        except Exception as e:
            raise AlpacaClientError(f"portfolio_history failed: {e}") from e

        equity_series = list(hist.equity or [])
        if len(equity_series) < 2:
            # Not enough history yet (new account). Return safe defaults.
            return PnlReading(
                daily_pnl_pct=Decimal("0"),
                weekly_pnl_pct=Decimal("0"),
                consecutive_losing_days=0,
                halted=False,
                halted_strategies=halted_strategies,
            )

        # Filter out None values (Alpaca returns None for non-trading days)
        clean = [e for e in equity_series if e is not None and e > 0]
        if len(clean) < 2:
            return PnlReading(
                daily_pnl_pct=Decimal("0"),
                weekly_pnl_pct=Decimal("0"),
                consecutive_losing_days=0,
                halted=False,
                halted_strategies=halted_strategies,
            )

        last = Decimal(str(clean[-1]))
        prev_day = Decimal(str(clean[-2]))
        week_open = Decimal(str(clean[0]))

        daily_pnl_pct = ((last - prev_day) / prev_day * Decimal("100")).quantize(Decimal("0.01"))
        weekly_pnl_pct = ((last - week_open) / week_open * Decimal("100")).quantize(Decimal("0.01"))

        # Count consecutive losing days from the tail
        consecutive_losses = 0
        for i in range(len(clean) - 1, 0, -1):
            if clean[i] < clean[i - 1]:
                consecutive_losses += 1
            else:
                break

        halted = False
        halt_reason = ""
        d_limit = Decimal(str(self._cfg.risk.daily_loss_limit_pct))
        w_limit = Decimal(str(self._cfg.risk.weekly_loss_limit_pct))
        if daily_pnl_pct <= -d_limit:
            halted = True
            halt_reason = f"daily P&L {daily_pnl_pct}% breaches -{d_limit}%"
        elif weekly_pnl_pct <= -w_limit:
            halted = True
            halt_reason = f"weekly P&L {weekly_pnl_pct}% breaches -{w_limit}%"

        # Bucket A: consecutive-losing-days throttle ladder.
        cap = int(self._cfg.risk.max_consecutive_losing_days)
        size_multiplier, ladder_reason = _throttle_ladder(consecutive_losses, cap)
        if ladder_reason and consecutive_losses >= cap + 2:
            halted = True
            if not halt_reason:
                halt_reason = ladder_reason

        return PnlReading(
            daily_pnl_pct=daily_pnl_pct,
            weekly_pnl_pct=weekly_pnl_pct,
            consecutive_losing_days=consecutive_losses,
            halted=halted,
            halt_reason=halt_reason,
            size_multiplier=size_multiplier,
            halted_strategies=halted_strategies,
        )

    def to_risk_state(self) -> RiskState:
        r = self.read()
        return RiskState(
            daily_pnl_pct=r.daily_pnl_pct,
            weekly_pnl_pct=r.weekly_pnl_pct,
            consecutive_losing_days=r.consecutive_losing_days,
            halted=r.halted,
            halted_strategies=r.halted_strategies,
            size_multiplier=r.size_multiplier,
        )


def _throttle_ladder(consecutive_losses: int, cap: int) -> tuple[Decimal, str]:
    """Map a losing-day streak to a size multiplier + human reason.

    Below cap: full size. At cap: half. cap+1: quarter. cap+2 and beyond:
    multiplier is 0.25 but the caller flips `halted=True` so trading stops.
    """
    if cap <= 0 or consecutive_losses < cap:
        return Decimal("1"), ""
    if consecutive_losses == cap:
        return Decimal("0.5"), f"{consecutive_losses} consecutive losing days at cap {cap}"
    if consecutive_losses == cap + 1:
        return Decimal("0.25"), f"{consecutive_losses} consecutive losing days (cap+1)"
    return Decimal("0.25"), f"{consecutive_losses} consecutive losing days — circuit breaker"
