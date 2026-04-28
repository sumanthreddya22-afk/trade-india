"""Backtest Engineer — Tier 5 lab role.

Wraps walk_forward_backtest and aggregates fold-level metrics into the
fitness inputs (alpha_vs_spy_x, sortino, max_dd_pct, folds_passed). Each
optuna trial calls this role once per (template, params) variant.
"""
from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

from sqlalchemy.orm import Session

from trading_bot.benchmark import SpyBenchmark
from trading_bot.fitness import promotion_gate_check, compute_fitness
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun
from trading_bot.walkforward import walk_forward_backtest

# Sentinel for "SPY returned ~zero" — clamp the alpha multiplier to avoid inf.
_ALPHA_INF_CLAMP = 100.0


class BacktestEngineerRole(BaseRole):
    name = "backtest_engineer"
    tier = 5
    process = "lab"
    job_description = (
        "Run 6-fold walk-forward backtest of a (template, params) variant. "
        "Returns fitness inputs (alpha_vs_spy_x, sortino, max_dd_pct, folds_passed)."
    )
    sla_seconds = 90
    upstream_roles: list[str] = []
    downstream_roles = ["param_optimizer", "promoter"]

    def _do_work(self, ctx):
        template = ctx["template"]
        params = ctx["params"]
        start = ctx["start"]
        end = ctx["end"]
        n_folds = ctx.get("n_folds", 6)

        results = walk_forward_backtest(
            template_name=template,
            params=params,
            start=start,
            end=end,
            n_folds=n_folds,
        )

        fold_alphas: list[float] = []
        fold_sortinos: list[float] = []
        fold_dds: list[float] = []
        folds_passed = 0

        for result in results:
            strat_ret = _strategy_return(result)
            spy_ret = _spy_period_return(result)
            alpha = _alpha_multiplier(strat_ret, spy_ret)
            sortino = _sortino_from_curve(result.equity_curve)
            max_dd = abs(_max_drawdown_pct(result.equity_curve))
            fold_alphas.append(alpha)
            fold_sortinos.append(sortino)
            fold_dds.append(max_dd)

            score = compute_fitness(
                alpha_vs_spy_x=alpha, sortino=sortino, max_dd_pct=max_dd
            )
            if promotion_gate_check(score):
                folds_passed += 1

        # Aggregate (mean for alpha/sortino, max for DD — worst-case)
        n = max(len(results), 1)
        # Most-recent fold's per-trade predictions, for Calibrator (Phase 3.5).
        per_trade: list[dict] = []
        if results:
            for t in getattr(results[-1], "trades", []):
                per_trade.append(
                    {
                        "symbol": t.symbol,
                        "entry_date": t.entry_date.isoformat(),
                        "predicted_pnl": float(t.realized_pnl),
                    }
                )
        return {
            "alpha_vs_spy_x": sum(fold_alphas) / n,
            "sortino": sum(fold_sortinos) / n,
            "max_dd_pct": max(fold_dds) if fold_dds else 0.0,
            "folds_passed": folds_passed,
            "folds_total": len(results),
            "per_trade_predictions": per_trade,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "backtests_run",
            float(count),
            f"{count} walk-forward backtests in last {lookback_days}d",
        )


def _strategy_return(result) -> float:
    if not result.equity_curve:
        return 0.0
    start_eq = result.starting_equity
    end_eq = result.ending_equity if result.ending_equity is not None else result.equity_curve[-1][1]
    if start_eq is None or Decimal(start_eq) <= 0:
        return 0.0
    return float(Decimal(end_eq) / Decimal(start_eq) - Decimal("1"))


def _spy_period_return(result) -> float:
    """Fetch SPY period return for the result's date span."""
    if not result.equity_curve:
        return 0.0
    start = result.equity_curve[0][0]
    end = result.equity_curve[-1][0]
    bench = SpyBenchmark()
    try:
        df = bench.get(start=start, end=end)
        return SpyBenchmark.period_return(df)
    except Exception:
        return 0.0


def _alpha_multiplier(strat_ret: float, spy_ret: float) -> float:
    """Alpha as a multiplier: 1.5 means strategy returned 1.5x SPY's return."""
    if abs(spy_ret) < 1e-6:
        # SPY flat: define alpha as the strategy return scaled to a sentinel,
        # clamped so optuna doesn't blow up on inf.
        if strat_ret > 0:
            return min(_ALPHA_INF_CLAMP, 1.0 + strat_ret * 100)
        return 0.0
    return strat_ret / spy_ret


def _sortino_from_curve(equity_curve) -> float:
    """Annualized Sortino = mean(daily) / downside_std(daily) * sqrt(252)."""
    if len(equity_curve) < 10:
        return 0.0
    rets: list[float] = []
    prev = equity_curve[0][1]
    for _, eq in equity_curve[1:]:
        if Decimal(prev) > 0:
            rets.append(float(Decimal(eq) / Decimal(prev) - Decimal("1")))
        prev = eq
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    downside = [r for r in rets if r < 0]
    if len(downside) < 2:
        return 0.0
    var = sum(r * r for r in downside) / len(downside)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(252)


def _max_drawdown_pct(equity_curve) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    worst = Decimal("0")
    for _, eq in equity_curve:
        if Decimal(eq) > Decimal(peak):
            peak = eq
        if Decimal(peak) > 0:
            dd = (Decimal(eq) / Decimal(peak) - Decimal("1")) * Decimal("100")
            if dd < worst:
                worst = dd
    return float(worst)
