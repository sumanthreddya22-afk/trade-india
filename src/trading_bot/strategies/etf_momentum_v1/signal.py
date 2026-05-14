"""ETF Momentum v1 — signal computation.

Pure function: ``signal_fn(history, decision_date) -> {symbol: weight}``.

Returns target weights summing to ≤ 1.0. Used identically by:
  * ``research.backtest.run_backtest`` (historical replay)
  * ``daemon.jobs.job_strategy_runner`` (live ticking)

No I/O, no globals, no side effects. The same inputs produce the same
outputs forever.
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Sequence

from trading_bot.research.historical_bars import DailyBar

STRATEGY_ID = "ETF_MOMENTUM_v1"

UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "EFA", "EEM",
    "XLK", "XLF", "XLE", "XLV",
)

# Operator-tunable parameters. The mutation engine reads its search
# space from research/search_space_v1.json; this dict is what the live
# strategy ticks with by default.
DEFAULT_PARAMS: dict[str, float | int] = {
    "lookback_days": 252,       # ≈ 12 months
    "skip_recent_days": 21,     # ≈ 1 month
    "top_n": 3,                 # equal-weight the top N
    "min_history_days": 250,    # require at least ~1y of history
    "min_positive_momentum": 0.0,   # only buy when 12-1 mo return > 0%
}


def _trailing_return(
    bars: Sequence[DailyBar], *, decision_date: dt.date,
    lookback_days: int, skip_recent_days: int,
    min_history_days: int,
) -> float | None:
    """12-1 month total return on adjusted close.

    Returns None if there's insufficient history for a clean signal
    (Plan v4: better to abstain than to fabricate).
    """
    if len(bars) < min_history_days:
        return None
    # We need the closing price at:
    #   t_end   = decision_date - skip_recent_days
    #   t_start = decision_date - lookback_days
    # …or the nearest-but-not-after bar in each case.
    t_end_target = decision_date - dt.timedelta(days=skip_recent_days)
    t_start_target = decision_date - dt.timedelta(days=lookback_days)
    by_date = {b.bar_date: b for b in bars}

    def _bar_on_or_before(target: dt.date) -> DailyBar | None:
        # Linear scan is fine — list is short and sorted-ish; falling
        # back to the nearest prior bar protects us against weekends /
        # holidays. We never look at a date *after* ``target`` (that
        # would be the look-ahead bug).
        candidate = None
        for b in bars:
            if b.bar_date > target:
                break
            candidate = b
        return candidate

    b_end = by_date.get(t_end_target) or _bar_on_or_before(t_end_target)
    b_start = by_date.get(t_start_target) or _bar_on_or_before(t_start_target)
    if b_end is None or b_start is None:
        return None
    if b_start.close <= 0:
        return None
    return b_end.close / b_start.close - 1.0


def signal_fn(
    history: Mapping[str, Sequence[DailyBar]],
    decision_date: dt.date,
    *,
    params: Mapping = DEFAULT_PARAMS,
    universe: Sequence[str] = UNIVERSE,
) -> dict[str, float]:
    """Compute target weights for ``decision_date``.

    Returns a dict ``{symbol: weight}``. Symbols not in the dict are
    implicitly zero-weight. The caller (backtest engine or daemon
    runner) handles converting these to orders.
    """
    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    skip = int(params.get("skip_recent_days", DEFAULT_PARAMS["skip_recent_days"]))
    top_n = int(params.get("top_n", DEFAULT_PARAMS["top_n"]))
    min_hist = int(params.get("min_history_days", DEFAULT_PARAMS["min_history_days"]))
    min_pos = float(params.get("min_positive_momentum",
                               DEFAULT_PARAMS["min_positive_momentum"]))

    scored: list[tuple[str, float]] = []
    for sym in universe:
        bars = history.get(sym) or ()
        # IMPORTANT: never look at bars dated AFTER decision_date
        # (this is the look-ahead guarantee — tested in
        # test_phase8_signal_no_lookahead).
        bars_until = [b for b in bars if b.bar_date <= decision_date]
        r = _trailing_return(
            bars_until, decision_date=decision_date,
            lookback_days=lookback, skip_recent_days=skip,
            min_history_days=min_hist,
        )
        if r is None or r <= min_pos:
            continue
        scored.append((sym, r))

    if not scored:
        return {}

    scored.sort(key=lambda x: x[1], reverse=True)
    winners = scored[:top_n]
    weight_each = 1.0 / max(1, top_n)
    return {sym: weight_each for sym, _ in winners}


__all__ = ["DEFAULT_PARAMS", "STRATEGY_ID", "UNIVERSE", "signal_fn"]
