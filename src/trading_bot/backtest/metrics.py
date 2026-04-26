"""Per-strategy / per-regime metric aggregation.

All inputs come from a finished `BacktestRunResult`. No external state.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from trading_bot.backtest.simulator import BacktestRunResult, BacktestTrade


@dataclass(frozen=True)
class SliceMetrics:
    n: int
    wins: int
    losses: int
    win_rate_pct: float | None
    gross_win: Decimal
    gross_loss: Decimal
    profit_factor: float | None  # None if no losses AND no wins; math.inf if no losses but wins
    expectancy: Decimal | None
    avg_hold_days: float | None
    sharpe_daily_ann: float | None
    max_drawdown_pct: float | None
    total_pnl: Decimal


@dataclass(frozen=True)
class BacktestMetrics:
    overall: SliceMetrics
    per_strategy: dict[str, SliceMetrics]
    per_strategy_regime: dict[tuple[str, str], SliceMetrics]
    per_asset_class: dict[str, SliceMetrics]
    dominant_regime: str
    regime_day_counts: dict[str, int]


def _empty_slice() -> SliceMetrics:
    return SliceMetrics(
        n=0, wins=0, losses=0, win_rate_pct=None,
        gross_win=Decimal("0"), gross_loss=Decimal("0"),
        profit_factor=None, expectancy=None,
        avg_hold_days=None, sharpe_daily_ann=None,
        max_drawdown_pct=None, total_pnl=Decimal("0"),
    )


def _slice(
    trades: list[BacktestTrade],
    daily_returns: list[float] | None = None,
    equity_curve: list[tuple[date, Decimal]] | None = None,
) -> SliceMetrics:
    if not trades:
        return _empty_slice()
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl < 0]
    n, nw, nl = len(trades), len(wins), len(losses)
    gw = sum((t.realized_pnl for t in wins), Decimal("0"))
    gl = abs(sum((t.realized_pnl for t in losses), Decimal("0")))
    total = sum((t.realized_pnl for t in trades), Decimal("0"))

    win_rate = (nw / n * 100) if n else None
    if gl > 0:
        pf: float | None = float(gw / gl)
    elif gw > 0:
        pf = math.inf
    else:
        pf = None
    expectancy = (total / n) if n else None
    avg_hold = sum(t.hold_days for t in trades) / n if n else None

    sharpe = None
    if daily_returns and len(daily_returns) >= 10:
        mean = sum(daily_returns) / len(daily_returns)
        var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std > 0:
            sharpe = (mean / std) * math.sqrt(252)

    max_dd = None
    if equity_curve:
        peak = equity_curve[0][1]
        worst = Decimal("0")
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                drawdown = (eq / peak - Decimal("1")) * Decimal("100")
                if drawdown < worst:
                    worst = drawdown
        max_dd = float(worst)

    return SliceMetrics(
        n=n, wins=nw, losses=nl,
        win_rate_pct=round(win_rate, 1) if win_rate is not None else None,
        gross_win=gw, gross_loss=gl,
        profit_factor=(round(pf, 2) if pf is not None and pf != math.inf else pf),
        expectancy=expectancy.quantize(Decimal("0.01")) if expectancy is not None else None,
        avg_hold_days=round(avg_hold, 1) if avg_hold is not None else None,
        sharpe_daily_ann=round(sharpe, 2) if sharpe is not None else None,
        max_drawdown_pct=round(max_dd, 2) if max_dd is not None else None,
        total_pnl=total.quantize(Decimal("0.01")),
    )


def _daily_returns(equity_curve: list[tuple[date, Decimal]]) -> list[float]:
    if len(equity_curve) < 2:
        return []
    out: list[float] = []
    prev = equity_curve[0][1]
    for _, eq in equity_curve[1:]:
        if prev > 0:
            out.append(float((eq / prev - Decimal("1"))))
        prev = eq
    return out


def compute_metrics(result: BacktestRunResult) -> BacktestMetrics:
    daily_rets = _daily_returns(result.equity_curve)

    overall = _slice(result.trades, daily_rets, result.equity_curve)

    per_strategy: dict[str, SliceMetrics] = {}
    for strat in {t.strategy for t in result.trades}:
        per_strategy[strat] = _slice(
            [t for t in result.trades if t.strategy == strat]
        )

    per_strategy_regime: dict[tuple[str, str], SliceMetrics] = {}
    for strat in {t.strategy for t in result.trades}:
        for regime in {t.regime_at_entry for t in result.trades if t.strategy == strat}:
            key = (strat, regime)
            per_strategy_regime[key] = _slice(
                [t for t in result.trades if t.strategy == strat and t.regime_at_entry == regime]
            )

    per_asset: dict[str, SliceMetrics] = {}
    for ac in {t.asset_class for t in result.trades}:
        per_asset[ac] = _slice([t for t in result.trades if t.asset_class == ac])

    # Dominant regime: the one with the most trades. (Lacking a per-day regime
    # stream, this is the simplest defensible measure.)
    regime_counts: dict[str, int] = {}
    for t in result.trades:
        regime_counts[t.regime_at_entry] = regime_counts.get(t.regime_at_entry, 0) + 1
    if regime_counts:
        dominant = max(regime_counts.items(), key=lambda kv: kv[1])[0]
    else:
        dominant = "unknown"

    return BacktestMetrics(
        overall=overall,
        per_strategy=per_strategy,
        per_strategy_regime=per_strategy_regime,
        per_asset_class=per_asset,
        dominant_regime=dominant,
        regime_day_counts=regime_counts,
    )
