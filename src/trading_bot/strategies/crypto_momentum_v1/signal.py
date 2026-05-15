"""Crypto Momentum signal — pick BTC or ETH by 90-day return.

The crypto sleeve cap (15% of equity) is applied at the runner level;
the signal returns a weight for the chosen asset relative to the
sleeve, not the full portfolio.
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Sequence

from trading_bot.research.historical_bars import DailyBar

STRATEGY_ID = "CRYPTO_MOMENTUM_v1"

# Alpaca crypto symbols use slash form on the data side.
UNIVERSE: tuple[str, ...] = ("BTC/USD", "ETH/USD")

# Read from risk_policy.lock at runtime; kept here as documented default.
CRYPTO_GROSS_MAX_PCT = 15.0

DEFAULT_PARAMS: dict = {
    "lookback_days": 90,
    "min_history_days": 95,
    # Fraction of crypto sleeve to allocate (1.0 = full sleeve into winner)
    "sleeve_weight": 1.0,
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
    """Return ``{winner_symbol: sleeve_weight}`` or {} if no clear winner.

    sleeve_weight defaults to 1.0 of the crypto sleeve; the runner caps
    that against CRYPTO_GROSS_MAX_PCT × equity.
    """
    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    min_hist = int(params.get("min_history_days", DEFAULT_PARAMS["min_history_days"]))
    sleeve_w = float(params.get("sleeve_weight", 1.0))

    returns: dict[str, float] = {}
    for sym in universe:
        bars = history.get(sym) or ()
        if len(bars) < min_hist:
            continue
        r = _trailing_return(bars, decision_date, lookback)
        if r is None or r <= 0:
            # Don't buy a crypto that's been negative for 90 days.
            continue
        returns[sym] = r

    if not returns:
        return {}

    winner = max(returns.items(), key=lambda kv: kv[1])[0]
    return {winner: sleeve_w}


__all__ = [
    "CRYPTO_GROSS_MAX_PCT", "DEFAULT_PARAMS",
    "STRATEGY_ID", "UNIVERSE", "signal_fn",
]
