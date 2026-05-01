"""30-day paper-trade alpha vs SPY computation.

Reads closed trades from `closed_trades.db` and SPY benchmark prices via
SpyBenchmark, returns the strategy's realized return divided by SPY's
return over the same window.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from trading_bot.benchmark import SpyBenchmark

INSUFFICIENT_TRADES_THRESHOLD = 5  # below this n, treat as no signal
ALPHA_INF_CLAMP = 100.0

# Notes substrings that mark an audit row, not an actual trade. These rows
# have realized_pnl=0 and entry_price==exit_price; counting them inflates
# n_trades and zeros out the strategy return, which can spuriously trip
# the strategy_coach fallback gate.
_AUDIT_NOTE_MARKERS = ("cancelled_unfilled", "reconciled_no_fill_found")


def _is_audit_row(notes: str | None) -> bool:
    n = (notes or "").lower()
    return any(m in n for m in _AUDIT_NOTE_MARKERS)


def compute_journal_alpha_vs_spy(
    *,
    closed_trades_db: str | Path = "data/closed_trades.db",
    starting_equity: Decimal = Decimal("15000"),
    lookback_days: int = 30,
    as_of: dt.date | None = None,
    benchmark: SpyBenchmark | None = None,
) -> dict:
    """Compute 30d realized alpha multiplier (strategy_ret / spy_ret).

    Returns:
        n_trades: number of closed trades inside the window
        strategy_return_pct: realized P&L / starting_equity, as a decimal
        spy_return_pct: SPY price return over same date span
        alpha_multiplier: strategy_return / spy_return, clamped at ±ALPHA_INF_CLAMP
        insufficient_data: True when n_trades < INSUFFICIENT_TRADES_THRESHOLD
    """
    as_of = as_of or dt.date.today()
    window_start = as_of - dt.timedelta(days=lookback_days)

    n_trades = 0
    realized = Decimal("0")
    cdb = Path(closed_trades_db)
    if cdb.exists():
        from trading_bot.reconciliation import ClosedTradeStore

        store = ClosedTradeStore(cdb)
        for t in store.all():
            if _is_audit_row(getattr(t, "notes", "")):
                continue  # cancelled/never-filled audit row — not a real trade
            exit_date = t.exit_time.date() if hasattr(t.exit_time, "date") else None
            if exit_date and window_start <= exit_date <= as_of:
                realized += t.realized_pnl
                n_trades += 1

    if n_trades < INSUFFICIENT_TRADES_THRESHOLD:
        return {
            "n_trades": n_trades,
            "strategy_return_pct": 0.0,
            "spy_return_pct": 0.0,
            "alpha_multiplier": 0.0,
            "insufficient_data": True,
        }

    strat_ret = float(realized / starting_equity) if starting_equity > 0 else 0.0

    bench = benchmark or SpyBenchmark()
    try:
        df = bench.get(start=window_start, end=as_of)
        spy_ret = SpyBenchmark.period_return(df)
    except Exception:
        spy_ret = 0.0

    if abs(spy_ret) < 1e-6:
        # SPY flat: alpha undefined; clamp to a sentinel so downstream comparisons work
        if strat_ret > 0:
            alpha = min(ALPHA_INF_CLAMP, 1.0 + strat_ret * 100)
        else:
            alpha = 0.0
    else:
        alpha = strat_ret / spy_ret

    return {
        "n_trades": n_trades,
        "strategy_return_pct": strat_ret,
        "spy_return_pct": spy_ret,
        "alpha_multiplier": alpha,
        "insufficient_data": False,
    }
