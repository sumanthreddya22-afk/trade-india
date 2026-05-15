"""Dual Momentum live runner."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, load_bars, open_store,
)
from trading_bot.strategies.dual_momentum_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, UNIVERSE, signal_fn,
)
from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)


def _max_sleeve_pct() -> float:
    """The most-binding cap among:
      * per_symbol_gross_max_pct  (most-binding for single-symbol strategies)
      * per_lane_allocation_max_pct  (40% default)
      * equity_gross_max_pct  (80% default)
    Apply a small safety buffer so we don't trip the kernel.
    """
    import json
    try:
        lock = json.loads((DEFAULT_POLICY_DIR / "risk_policy.lock").read_text())
    except Exception:
        return 0.04   # 4% safe default
    per_sym = float(lock.get("symbol", {}).get("per_symbol_gross_max_pct", 5.0))
    per_lane = float(lock.get("lane", {}).get("per_lane_allocation_max_pct", 40.0))
    asset = float(lock.get("asset_class", {}).get("equity_gross_max_pct", 80.0))
    most_binding = min(per_sym, per_lane, asset)
    # 10% buffer below the binding cap.
    return max(0.0, most_binding * 0.90) / 100.0


@dataclass(frozen=True)
class StrategyDecision:
    decision_date: dt.date
    target_weights: dict[str, float]
    current_qty: dict[str, float]
    equity: float
    intents: list[dict]


def should_rebalance_today(today: dt.date, last_date: dt.date | None) -> bool:
    """Monthly cadence: first trading day we see this calendar month."""
    if last_date is None:
        return True
    return (today.year, today.month) != (last_date.year, last_date.month)


def evaluate_strategy(
    *,
    historical_db: Path = DEFAULT_HISTORICAL_PATH,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
) -> StrategyDecision:
    decision_date = decision_date or dt.date.today()
    if not historical_db.exists():
        return StrategyDecision(
            decision_date=decision_date,
            target_weights={}, current_qty={}, equity=0.0, intents=[],
        )

    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    min_hist = int(params.get("min_history_days", DEFAULT_PARAMS["min_history_days"]))
    # Need at least min_hist TRADING days. Calendar buffer = ×1.5 + 30
    # to cover weekends + market holidays.
    start = decision_date - dt.timedelta(days=int(max(lookback, min_hist) * 1.5) + 30)

    conn = open_store(historical_db)
    try:
        bars = load_bars(conn, symbols=UNIVERSE, start=start, end=decision_date)
    finally:
        conn.close()

    target_weights = signal_fn(bars, decision_date, params=params)

    current_qty: dict[str, float] = {}
    if positions_fetcher is not None:
        for p in positions_fetcher() or []:
            current_qty[p["symbol"]] = float(p.get("qty", 0))
    equity = 0.0
    if account_fetcher is not None:
        equity = float((account_fetcher() or {}).get("equity", 0.0))

    intents: list[dict] = []
    # Sleeve cap: most-binding cap from risk_policy.lock with a 10%
    # buffer. For single-symbol strategies on the v4 default lock this
    # is 4.5% (= 5% per_symbol × 0.9). Operator-tunable via the lock.
    sleeve_cap = _max_sleeve_pct()
    if target_weights and equity > 0:
        close_by_sym: dict[str, float] = {}
        for sym, series in bars.items():
            relevant = [b for b in series if b.bar_date <= decision_date]
            if relevant:
                close_by_sym[sym] = relevant[-1].close

        # Convert weights to qty diffs.
        for sym, w in target_weights.items():
            close = close_by_sym.get(sym)
            if not close or close <= 0:
                continue
            target_qty = (equity * sleeve_cap * w) / close
            diff = target_qty - current_qty.get(sym, 0.0)
            if abs(diff) < 1e-3:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID, "strategy_ver": 1,
                "symbol": sym, "side": "buy" if diff > 0 else "sell",
                "qty": abs(diff), "intent_price": close,
                "asset_class": "us_equity", "lane": "dual_momentum",
                "rationale": f"dual-momentum winner: {sym} (weight={w:.3f})",
            })
        # Sell any held symbol not in target.
        for sym, qty in current_qty.items():
            if sym in target_weights or qty <= 0 or sym not in UNIVERSE:
                continue
            close = close_by_sym.get(sym, 0.0)
            if close <= 0:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID, "strategy_ver": 1,
                "symbol": sym, "side": "sell", "qty": qty,
                "intent_price": close, "asset_class": "us_equity",
                "lane": "dual_momentum",
                "rationale": "dual-momentum: rotate out",
            })

    return StrategyDecision(
        decision_date=decision_date,
        target_weights=dict(target_weights),
        current_qty=current_qty, equity=equity, intents=intents,
    )


__all__ = ["StrategyDecision", "evaluate_strategy", "should_rebalance_today"]
