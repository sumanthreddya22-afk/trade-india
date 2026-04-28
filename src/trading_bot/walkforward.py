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


# Stable baseline if opportunities.md is missing/empty. Diverse large-caps so
# the optimizer can discover real alpha rather than just SPY-on-SPY.
_BASELINE_LAB_UNIVERSE = (
    "SPY", "QQQ", "AAPL", "MSFT", "AMD",
    "NVDA", "GOOGL", "META", "JPM", "JNJ",
)
LAB_UNIVERSE_TOP_N = 25


def _lab_universe(opportunities_path: "Path | None" = None) -> list[str]:
    """Resolve the lab's backtest universe from the live opportunities feed.

    The Stock Scanner trades whatever the Universe Curator ranked into
    `strategy/opportunities.md` at 07:30 ET. The lab should optimize params
    over that same universe — otherwise it solves the wrong problem.

    Crypto symbols are filtered out: walk-forward backtests use daily bars,
    and the crypto book trades 24/7 with different risk treatment.

    Falls back to the baseline list when the opportunities file is missing
    or contains no stocks (e.g. fresh install before the curator has run).
    """
    from pathlib import Path

    from trading_bot.orchestrator import load_ranked_watchlist

    path = opportunities_path or Path("strategy/opportunities.md")
    ranked = load_ranked_watchlist(path)
    stocks = [e.symbol for e in ranked if e.asset_class != "crypto"]
    universe = stocks[:LAB_UNIVERSE_TOP_N]
    if not universe:
        return list(_BASELINE_LAB_UNIVERSE)
    return universe


def _ensure_bars_warmed(
    bar_store, *, symbols: list[str], from_date: dt.date, to_date: dt.date
) -> list[str]:
    """Make sure each symbol has bars cached over [from_date, to_date].

    Returns the list of symbols that have usable cached data after any
    auto-backfill. Symbols whose Alpaca fetch fails are dropped from the
    returned list so the simulator doesn't waste fold time on empty bars.
    """
    from trading_bot.config import Settings
    from trading_bot.market_data import MarketDataClient

    missing: list[str] = []
    for sym in symbols:
        if not bar_store.is_warm(sym, from_date=from_date, to_date=to_date):
            missing.append(sym)
    if not missing:
        return symbols

    market = MarketDataClient(Settings())
    results = bar_store.warm(
        missing, from_date=from_date, to_date=to_date, market=market
    )
    # Drop symbols whose fetch failed (warm() returns -1 for them).
    failed = {sym for sym, n in results.items() if n == -1}
    return [s for s in symbols if s not in failed]


def _run_simulator(
    *, template_name: str, params: dict, fold: FoldDefinition
) -> BacktestRunResult:
    """Run the existing Backtester for one fold's test window with the given params."""
    from pathlib import Path
    from trading_bot.backtest.bar_store import BarStore
    from trading_bot.backtest.simulator import Backtester, fetch_vix_history
    from trading_bot.config import load_config

    if template_name != "momentum":
        raise ValueError(f"Unknown template: {template_name}")

    cfg = load_config(Path("strategy/config.yaml"))
    # Allow explicit override via config (advanced); otherwise pull live universe.
    explicit = list(getattr(cfg, "lab_backtest_universe", []) or [])
    universe = explicit or _lab_universe()
    bar_store = BarStore(db_path="data/massive_grouped.db")
    # Auto-backfill any symbols that aren't in the cache yet — keeps the lab
    # tracking the rotating live universe without manual `bot lab-backfill`.
    universe = _ensure_bars_warmed(
        bar_store, symbols=universe, from_date=fold.train_start, to_date=fold.test_end
    )
    if not universe:
        # All fetches failed (e.g. Alpaca down). Skip this fold rather than
        # crash — caller observes a no-trade BacktestRunResult.
        from trading_bot.backtest.simulator import _Portfolio
        from datetime import datetime, timezone
        from decimal import Decimal
        import uuid as _uuid
        return BacktestRunResult(
            run_id=_uuid.uuid4().hex[:12],
            generated_at=datetime.now(timezone.utc),
            from_date=fold.test_start, to_date=fold.test_end,
            symbols=[], strategies_used=["momentum"],
            equity_curve=[], starting_equity=Decimal("15000"),
        )
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
