"""Dual Momentum SPY-vs-TLT signal.

Pure function: ``signal_fn(history, decision_date) -> {symbol: weight}``.
Returns {SPY: 1.0} or {TLT: 1.0} — never mixed, never empty when both
series have history.
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Sequence

from trading_bot.research.historical_bars import DailyBar

STRATEGY_ID = "DUAL_MOMENTUM_v1"

UNIVERSE: tuple[str, ...] = ("SPY", "TLT")

DEFAULT_PARAMS: dict = {
    "lookback_days": 90,
    "min_history_days": 95,
}


def _trailing_return(
    bars: Sequence[DailyBar], decision_date: dt.date, lookback: int,
) -> float | None:
    bars_until = [b for b in bars if b.bar_date <= decision_date]
    if not bars_until:
        return None
    t_start = decision_date - dt.timedelta(days=lookback)
    end_close = bars_until[-1].close
    start_bar = None
    for b in bars_until:
        if b.bar_date <= t_start:
            start_bar = b
        else:
            break
    if start_bar is None or start_bar.close <= 0:
        return None
    return end_close / start_bar.close - 1.0


def signal_fn(
    history: Mapping[str, Sequence[DailyBar]],
    decision_date: dt.date,
    *,
    params: Mapping = DEFAULT_PARAMS,
    universe: Sequence[str] = UNIVERSE,
) -> dict[str, float]:
    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    min_hist = int(params.get("min_history_days", DEFAULT_PARAMS["min_history_days"]))

    returns: dict[str, float] = {}
    for sym in universe:
        bars = history.get(sym) or ()
        if len(bars) < min_hist:
            continue
        r = _trailing_return(bars, decision_date, lookback)
        if r is None:
            continue
        returns[sym] = r

    if not returns:
        return {}

    winner = max(returns.items(), key=lambda kv: kv[1])[0]
    return {winner: 1.0}


__all__ = ["DEFAULT_PARAMS", "STRATEGY_ID", "UNIVERSE", "signal_fn"]
