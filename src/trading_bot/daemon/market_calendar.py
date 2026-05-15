"""US equity market calendar — full closes + early closes.

Hand-curated through 2027 so the daemon doesn't pull in a heavy
``pandas_market_calendars`` dependency for one feature. The list MUST
be re-published before 2028 (NYSE publishes annual schedules in
December of the prior year).

Sources cross-checked:
  - https://www.nyse.com/markets/hours-calendars
  - https://www.nasdaqtrader.com/Trader.aspx?id=Calendar

Half-day early closes (13:00 ET) are listed separately. The strategy
dispatch loop must treat a half-day as a closed day for cadence
purposes — rebalance orders need full liquidity windows.
"""
from __future__ import annotations

import datetime as dt
from typing import Final


# ---------------------------------------------------------------------------
# Full closures
# ---------------------------------------------------------------------------

_FULL_CLOSURES: Final[set[dt.date]] = {
    # 2025 retained for historical replay / backtest re-runs.
    dt.date(2025, 1, 1), dt.date(2025, 1, 20), dt.date(2025, 2, 17),
    dt.date(2025, 4, 18), dt.date(2025, 5, 26), dt.date(2025, 6, 19),
    dt.date(2025, 7, 4), dt.date(2025, 9, 1), dt.date(2025, 11, 27),
    dt.date(2025, 12, 25),
    # 2026
    dt.date(2026, 1, 1),     # New Year's Day (Thu)
    dt.date(2026, 1, 19),    # MLK Day (Mon)
    dt.date(2026, 2, 16),    # Presidents Day (Mon)
    dt.date(2026, 4, 3),     # Good Friday (Fri)
    dt.date(2026, 5, 25),    # Memorial Day (Mon)
    dt.date(2026, 6, 19),    # Juneteenth (Fri)
    dt.date(2026, 7, 3),     # Independence Day observed (Fri; Jul 4 = Sat)
    dt.date(2026, 9, 7),     # Labor Day (Mon)
    dt.date(2026, 11, 26),   # Thanksgiving (Thu)
    dt.date(2026, 12, 25),   # Christmas (Fri)
    # 2027
    dt.date(2027, 1, 1),     # New Year's (Fri)
    dt.date(2027, 1, 18),    # MLK
    dt.date(2027, 2, 15),    # Presidents
    dt.date(2027, 3, 26),    # Good Friday
    dt.date(2027, 5, 31),    # Memorial
    dt.date(2027, 6, 18),    # Juneteenth observed (Jun 19 = Sat)
    dt.date(2027, 7, 5),     # Independence observed (Jul 4 = Sun)
    dt.date(2027, 9, 6),     # Labor
    dt.date(2027, 11, 25),   # Thanksgiving
    dt.date(2027, 12, 24),   # Christmas observed (Dec 25 = Sat)
}

# Half-day early closes (13:00 ET). The dispatch loop treats these
# the same as full closes for rebalance cadence; intraday strategies
# may still tick but with the half-day flag set.
_EARLY_CLOSES: Final[set[dt.date]] = {
    dt.date(2026, 7, 2),     # day before Independence Day
    dt.date(2026, 11, 27),   # day after Thanksgiving
    dt.date(2026, 12, 24),   # Christmas Eve (Thu)
    dt.date(2027, 7, 2),     # day before Independence (Fri)
    dt.date(2027, 11, 26),   # day after Thanksgiving
    dt.date(2027, 12, 23),   # Christmas Eve observed (Dec 24 = Fri)
}


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

CALENDAR_HORIZON_END: Final[dt.date] = dt.date(2027, 12, 31)
"""The last date covered by the curated calendar. Beyond this the
predicates raise ``CalendarHorizonExceeded`` rather than silently
treating future dates as trading days."""


class CalendarHorizonExceeded(RuntimeError):
    """The market calendar is curated through ``CALENDAR_HORIZON_END``.
    Past that the data is unknown and the daemon must halt strategy
    cadence rather than guess. Re-publish the calendar annually."""


def is_full_close(d: dt.date) -> bool:
    _check_horizon(d)
    return d in _FULL_CLOSURES


def is_early_close(d: dt.date) -> bool:
    _check_horizon(d)
    return d in _EARLY_CLOSES


def is_weekend(d: dt.date) -> bool:
    return d.weekday() >= 5


def is_us_equity_trading_day(d: dt.date) -> bool:
    """True iff the NYSE/NASDAQ is open for a full session on ``d``."""
    _check_horizon(d)
    if is_weekend(d):
        return False
    return not (is_full_close(d) or is_early_close(d))


def previous_trading_day(d: dt.date) -> dt.date:
    """Walk back until a full trading day; useful for cadence anchors."""
    cur = d - dt.timedelta(days=1)
    while not is_us_equity_trading_day(cur):
        cur = cur - dt.timedelta(days=1)
    return cur


def next_trading_day(d: dt.date) -> dt.date:
    """Walk forward until a full trading day."""
    cur = d + dt.timedelta(days=1)
    while not is_us_equity_trading_day(cur):
        cur = cur + dt.timedelta(days=1)
    return cur


def is_first_trading_day_of_month(d: dt.date) -> bool:
    """True iff ``d`` is a full trading day AND every preceding day in
    the same calendar month is closed (weekend or holiday). Replaces
    naive "day == 1" cadence checks in the strategies."""
    if not is_us_equity_trading_day(d):
        return False
    cur = d - dt.timedelta(days=1)
    while cur.month == d.month:
        if is_us_equity_trading_day(cur):
            return False
        cur = cur - dt.timedelta(days=1)
    return True


def _check_horizon(d: dt.date) -> None:
    if d > CALENDAR_HORIZON_END:
        raise CalendarHorizonExceeded(
            f"date {d} exceeds curated calendar horizon "
            f"{CALENDAR_HORIZON_END}. Publish a new calendar before "
            f"trading past this date."
        )


__all__ = [
    "CALENDAR_HORIZON_END",
    "CalendarHorizonExceeded",
    "is_early_close",
    "is_first_trading_day_of_month",
    "is_full_close",
    "is_us_equity_trading_day",
    "is_weekend",
    "next_trading_day",
    "previous_trading_day",
]
