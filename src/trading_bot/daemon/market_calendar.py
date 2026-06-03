"""NSE/BSE equity market calendar — full closes (exchange holidays).

Hand-curated through 2027. The list MUST be re-published before 2028
(NSE publishes the annual holiday schedule in December of the prior year).

Sources cross-checked:
  - https://www.nseindia.com/products-services/equity-market-holidays
  - https://www.bseindia.com/markets/equity/EQReports/StockHoldingReport.html

NSE and BSE share the same holiday list for equities. Muhurat Trading
(Diwali special session) is treated as a full close for strategy
cadence — the session is too short for systematic rebalance orders.

There are NO half-days / early-close sessions on NSE (unlike NYSE);
every trading day runs a full 09:15–15:30 IST session.
"""
from __future__ import annotations

import datetime as dt
from typing import Final


# ---------------------------------------------------------------------------
# Full closures (NSE/BSE equity segment)
# ---------------------------------------------------------------------------

_FULL_CLOSURES: Final[set[dt.date]] = {
    # ---- 2025 (retained for historical replay / backtest re-runs) ----
    dt.date(2025, 1, 26),   # Republic Day
    dt.date(2025, 2, 26),   # Mahashivratri
    dt.date(2025, 3, 14),   # Holi
    dt.date(2025, 3, 31),   # Id-Ul-Fitr (Ramzan Eid)
    dt.date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    dt.date(2025, 4, 18),   # Good Friday
    dt.date(2025, 5, 1),    # Maharashtra Day
    dt.date(2025, 8, 15),   # Independence Day
    dt.date(2025, 8, 27),   # Ganesh Chaturthi
    dt.date(2025, 10, 2),   # Gandhi Jayanti
    dt.date(2025, 10, 24),  # Dussehra
    dt.date(2025, 11, 5),   # Diwali Laxmi Puja (Muhurat Trading — full close for cadence)
    dt.date(2025, 11, 15),  # Gurunanak Jayanti
    dt.date(2025, 12, 25),  # Christmas

    # ---- 2026 ----
    dt.date(2026, 1, 26),   # Republic Day (Mon)
    dt.date(2026, 2, 16),   # Mahashivratri (Mon) — tentative
    dt.date(2026, 3, 3),    # Holi (Tue)
    dt.date(2026, 3, 20),   # Id-Ul-Fitr (Fri) — tentative, moon-sighting dependent
    dt.date(2026, 4, 3),    # Good Friday (Fri)
    dt.date(2026, 4, 14),   # Dr. Ambedkar Jayanti (Tue)
    dt.date(2026, 5, 1),    # Maharashtra Day (Fri)
    dt.date(2026, 9, 17),   # Ganesh Chaturthi (Thu) — tentative
    dt.date(2026, 10, 2),   # Gandhi Jayanti (Fri)
    dt.date(2026, 10, 13),  # Dussehra (Tue) — tentative
    dt.date(2026, 11, 3),   # Gurunanak Jayanti (Tue) — tentative
    dt.date(2026, 12, 25),  # Christmas (Fri)

    # ---- 2027 ----
    dt.date(2027, 1, 26),   # Republic Day (Tue)
    dt.date(2027, 2, 5),    # Mahashivratri (Fri) — tentative
    dt.date(2027, 3, 9),    # Id-Ul-Fitr — tentative
    dt.date(2027, 3, 22),   # Holi (Mon)
    dt.date(2027, 3, 26),   # Good Friday
    dt.date(2027, 4, 14),   # Dr. Ambedkar Jayanti (Wed)
    dt.date(2027, 9, 6),    # Ganesh Chaturthi (Mon) — tentative
    dt.date(2027, 10, 4),   # Dussehra (Mon) — tentative
    dt.date(2027, 11, 12),  # Diwali Laxmi Puja — tentative
    dt.date(2027, 11, 23),  # Gurunanak Jayanti (Tue) — tentative
}

# NSE has no early-close / half-day sessions (unlike NYSE).
# Kept empty for API compatibility.
_EARLY_CLOSES: Final[set[dt.date]] = set()


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

CALENDAR_HORIZON_END: Final[dt.date] = dt.date(2027, 12, 31)
"""Last date covered by the curated calendar. Beyond this the
predicates raise ``CalendarHorizonExceeded`` rather than silently
treating future dates as trading days. Re-publish annually from
https://www.nseindia.com/products-services/equity-market-holidays"""


class CalendarHorizonExceeded(RuntimeError):
    """The market calendar is curated through ``CALENDAR_HORIZON_END``.
    Past that the data is unknown and the daemon must halt strategy
    cadence rather than guess."""


def is_full_close(d: dt.date) -> bool:
    _check_horizon(d)
    return d in _FULL_CLOSURES


def is_early_close(d: dt.date) -> bool:
    """NSE has no early-close sessions. Always returns False.
    Kept for API compatibility with callers that test both predicates."""
    _check_horizon(d)
    return False


def is_weekend(d: dt.date) -> bool:
    return d.weekday() >= 5


def is_nse_trading_day(d: dt.date) -> bool:
    """True iff NSE equity segment is open for a full session on ``d``."""
    _check_horizon(d)
    if is_weekend(d):
        return False
    return not is_full_close(d)


# Backward-compat alias for any code that calls is_us_equity_trading_day.
is_us_equity_trading_day = is_nse_trading_day


def previous_trading_day(d: dt.date) -> dt.date:
    """Walk back until a full NSE trading day; useful for cadence anchors."""
    cur = d - dt.timedelta(days=1)
    while not is_nse_trading_day(cur):
        cur = cur - dt.timedelta(days=1)
    return cur


def next_trading_day(d: dt.date) -> dt.date:
    """Walk forward until a full NSE trading day."""
    cur = d + dt.timedelta(days=1)
    while not is_nse_trading_day(cur):
        cur = cur + dt.timedelta(days=1)
    return cur


def is_first_trading_day_of_month(d: dt.date) -> bool:
    """True iff ``d`` is a full NSE trading day AND every preceding day
    in the same calendar month is closed (weekend or holiday)."""
    if not is_nse_trading_day(d):
        return False
    cur = d - dt.timedelta(days=1)
    while cur.month == d.month:
        if is_nse_trading_day(cur):
            return False
        cur = cur - dt.timedelta(days=1)
    return True


def _check_horizon(d: dt.date) -> None:
    if d > CALENDAR_HORIZON_END:
        raise CalendarHorizonExceeded(
            f"date {d} exceeds curated NSE calendar horizon "
            f"{CALENDAR_HORIZON_END}. Publish a new calendar before "
            f"trading past this date."
        )


__all__ = [
    "CALENDAR_HORIZON_END",
    "CalendarHorizonExceeded",
    "is_early_close",
    "is_first_trading_day_of_month",
    "is_full_close",
    "is_nse_trading_day",
    "is_us_equity_trading_day",
    "is_weekend",
    "next_trading_day",
    "previous_trading_day",
]
