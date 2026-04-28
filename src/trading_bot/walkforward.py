"""Walk-forward backtest harness.

Splits a date range into N folds: each (train_window train, test_window test).
Test windows do not overlap; the cursor walks forward by `test_months` per fold.

Returns one BacktestRunResult per fold (TEST window only). The MomentumStrategy
is state-free, so we just run the test-window simulation directly. Train windows
remain in the API for future templates that need warm-up state.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from trading_bot.backtest.simulator import BacktestRunResult


@dataclass
class FoldDefinition:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def default_folds(
    *,
    start: dt.date,
    end: dt.date,
    n_folds: int = 6,
    train_months: int = 12,
    test_months: int = 3,
) -> list[FoldDefinition]:
    """Returns up to N folds with `train_months` train + `test_months` test,
    walking forward by `test_months`. Truncates if the range can't fit N."""
    folds: list[FoldDefinition] = []
    cursor = start
    for _ in range(n_folds):
        train_end = _add_months(cursor, train_months) - dt.timedelta(days=1)
        test_start = train_end + dt.timedelta(days=1)
        test_end = _add_months(test_start, test_months) - dt.timedelta(days=1)
        if test_end > end:
            break
        folds.append(
            FoldDefinition(
                train_start=cursor,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        cursor = _add_months(cursor, test_months)
    return folds


def _add_months(d: dt.date, months: int) -> dt.date:
    month_total = (d.year * 12) + (d.month - 1) + months
    new_year = month_total // 12
    new_month = (month_total % 12) + 1
    if new_month == 12:
        next_first = dt.date(new_year + 1, 1, 1)
    else:
        next_first = dt.date(new_year, new_month + 1, 1)
    days_in_month = (next_first - dt.timedelta(days=1)).day
    return dt.date(new_year, new_month, min(d.day, days_in_month))


def _run_simulator(
    *, template_name: str, params: dict, fold: FoldDefinition
) -> BacktestRunResult:
    """Run the existing Backtester for one fold's test window with the given params."""
    from trading_bot.backtest.bar_store import BarStore
    from trading_bot.backtest.simulator import Backtester, fetch_vix_history
    from trading_bot.config import load_config

    if template_name != "momentum":
        raise ValueError(f"Unknown template: {template_name}")

    cfg = load_config()
    universe = list(getattr(cfg, "lab_backtest_universe", []) or [])
    if not universe:
        # Fall back to a small SPY-tracking subset; real config supplies via
        # `lab_backtest_universe`. Phase 3 universe wiring lands at integration.
        universe = ["SPY"]
    bar_store = BarStore(db_path="data/massive_grouped.db")
    vix = fetch_vix_history(fold.test_start, fold.test_end)
    bt = Backtester(
        cfg,
        bar_store,
        vix_series=vix,
        strategy_overrides={"momentum": params},
    )
    return bt.run(
        from_date=fold.test_start,
        to_date=fold.test_end,
        symbols=universe,
        strategy_names=("momentum",),
    )


def walk_forward_backtest(
    *,
    template_name: str,
    params: dict[str, Any],
    start: dt.date,
    end: dt.date,
    n_folds: int = 6,
) -> list[BacktestRunResult]:
    folds = default_folds(start=start, end=end, n_folds=n_folds)
    results: list[BacktestRunResult] = []
    for fold in folds:
        results.append(_run_simulator(template_name=template_name, params=params, fold=fold))
    return results
