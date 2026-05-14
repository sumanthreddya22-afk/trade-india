"""Live runner — converts signal_fn output to OrderIntents.

Called daily by the daemon's ``job_strategy_runner``. The runner:

  1. Reads current positions + cash from the broker (via the daemon
     context's ``positions_fetcher`` / ``account_fetcher``).
  2. Loads the most-recent ``lookback_days + skip_recent_days + buffer``
     bars from ``historical_bars.db`` (the same store the backtest
     used).
  3. Calls ``signal_fn`` for ``decision_date = today``.
  4. Diff target weights vs current positions → enqueue OrderIntents.
  5. Submits each intent via ``execution.order_router.submit_order``.

Decisions only run on a configurable cadence (default monthly first
trading day), so the daemon's daily tick is a no-op on most days.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, DailyBar, load_bars, open_store,
)
from trading_bot.strategies.etf_momentum_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE, signal_fn,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyDecision:
    decision_date: dt.date
    target_weights: dict[str, float]
    current_qty: dict[str, float]
    equity: float
    intents: list[dict]               # OrderIntent-shaped dicts


def _last_trading_day_of_month_change(d: dt.date, prev: dt.date | None) -> bool:
    """True iff ``d`` is in a different calendar month than ``prev``.

    Used as the default "is this a rebalance day?" predicate. The first
    trading day of each calendar month triggers a rebalance.
    """
    if prev is None:
        return True
    return d.month != prev.month or d.year != prev.year


def _read_last_decision_date(
    ledger_conn: sqlite3.Connection, strategy_id: str,
) -> Optional[dt.date]:
    """Look up the most recent decision_ts for this strategy from
    strategy_decision. If none, return None (first-ever tick)."""
    try:
        cur = ledger_conn.execute(
            "SELECT MAX(decision_ts) FROM strategy_decision "
            "WHERE strategy_id=?",
            (strategy_id,),
        )
        row = cur.fetchone()
        if row and row[0]:
            return dt.datetime.fromisoformat(row[0]).date()
    except sqlite3.Error:
        return None
    return None


def evaluate_strategy(
    *,
    historical_db: Path = DEFAULT_HISTORICAL_PATH,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    universe: Sequence[str] = UNIVERSE,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
) -> StrategyDecision:
    """Compute the decision for ``decision_date``.

    This is a *pure-ish* function w.r.t. the broker fetchers: it reads
    state but doesn't submit. Call ``submit_intents`` separately to
    actually emit orders. This makes it dry-runnable for the dashboard.
    """
    decision_date = decision_date or dt.date.today()
    if not historical_db.exists():
        return StrategyDecision(
            decision_date=decision_date,
            target_weights={}, current_qty={}, equity=0.0, intents=[],
        )
    conn = open_store(historical_db)
    try:
        # Pull just enough history: lookback + skip + 30-day buffer.
        lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
        skip = int(params.get("skip_recent_days", DEFAULT_PARAMS["skip_recent_days"]))
        history_days = lookback + skip + 30
        start = decision_date - dt.timedelta(days=history_days)
        bars = load_bars(
            conn, symbols=tuple(universe), start=start, end=decision_date,
        )
    finally:
        conn.close()

    target_weights = signal_fn(bars, decision_date, params=params,
                                universe=universe)

    # Read current positions + equity.
    current_qty: dict[str, float] = {}
    equity = 0.0
    if positions_fetcher is not None:
        for p in positions_fetcher() or []:
            sym = p.get("symbol", "")
            if sym:
                current_qty[sym] = float(p.get("qty", 0))
    if account_fetcher is not None:
        acct = account_fetcher() or {}
        equity = float(acct.get("equity", 0.0))

    # Build OrderIntents. We need a recent close price per target
    # symbol; reuse the loaded bars.
    intents: list[dict] = []
    if target_weights and equity > 0:
        # close-price-as-of-decision_date per symbol
        close_by_sym: dict[str, float] = {}
        for sym, series in bars.items():
            relevant = [b for b in series if b.bar_date <= decision_date]
            if relevant:
                close_by_sym[sym] = relevant[-1].close

        for sym, w in target_weights.items():
            close = close_by_sym.get(sym)
            if not close or close <= 0:
                continue
            target_value = equity * w
            target_qty = target_value / close
            current = current_qty.get(sym, 0.0)
            diff = target_qty - current
            if abs(diff) < 1e-3:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": 1,
                "symbol": sym,
                "side": "buy" if diff > 0 else "sell",
                "qty": abs(diff),
                "intent_price": close,
                "asset_class": "us_equity",
                "lane": "etf_momentum",
                "rationale": f"rebalance to weight={w:.3f}",
            })
        # Symbols held but not in target → sell to zero.
        for sym, qty in current_qty.items():
            if sym in target_weights or qty <= 0:
                continue
            close = close_by_sym.get(sym, 0.0) or 0.0
            if close <= 0:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": 1,
                "symbol": sym,
                "side": "sell",
                "qty": qty,
                "intent_price": close,
                "asset_class": "us_equity",
                "lane": "etf_momentum",
                "rationale": "rebalance: dropped from target",
            })

    return StrategyDecision(
        decision_date=decision_date,
        target_weights=dict(target_weights),
        current_qty=current_qty, equity=equity, intents=intents,
    )


def should_rebalance_today(
    today: dt.date, last_decision_date: Optional[dt.date],
) -> bool:
    """Monthly cadence: rebalance when the month changes (i.e., today
    is the first trading day we've seen in this calendar month)."""
    if last_decision_date is None:
        return True
    return (today.year, today.month) != (last_decision_date.year, last_decision_date.month)


__all__ = [
    "StrategyDecision", "evaluate_strategy", "should_rebalance_today",
]
