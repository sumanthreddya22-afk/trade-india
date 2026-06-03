"""NSE/BSE market calendar — full closes, no half-days, first-trading-day."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.daemon.market_calendar import (
    CALENDAR_HORIZON_END,
    CalendarHorizonExceeded,
    is_early_close,
    is_first_trading_day_of_month,
    is_full_close,
    is_nse_trading_day,
    is_us_equity_trading_day,
    next_trading_day,
    previous_trading_day,
)


def test_republic_day_2026_is_closed() -> None:
    # 2026-01-26 = Republic Day (Mon).
    assert is_full_close(dt.date(2026, 1, 26))
    assert not is_nse_trading_day(dt.date(2026, 1, 26))


def test_maharashtra_day_2026_is_closed() -> None:
    # 2026-05-01 = Maharashtra Day (Fri).
    assert is_full_close(dt.date(2026, 5, 1))


def test_christmas_2026_is_closed() -> None:
    # NSE closes for Christmas same as NYSE — Dec 25 2026 (Fri).
    assert is_full_close(dt.date(2026, 12, 25))


def test_regular_tuesday_is_trading_day() -> None:
    # 2026-05-19 = Tue (no holiday).
    assert is_nse_trading_day(dt.date(2026, 5, 19))


def test_weekend_is_not_trading_day() -> None:
    # 2026-05-16 is a Saturday.
    assert not is_nse_trading_day(dt.date(2026, 5, 16))


def test_nse_has_no_half_days() -> None:
    """Unlike NYSE, NSE/BSE do not run half-day sessions. The
    is_early_close predicate is kept for API compat and always returns
    False."""
    # Pick a few candidate days that would have been NYSE half-days.
    assert not is_early_close(dt.date(2026, 11, 27))
    assert not is_early_close(dt.date(2026, 12, 24))
    # Christmas Eve 2026 is a Thu and (per the NSE calendar) a regular
    # trading day — only Dec 25 is closed.
    assert is_nse_trading_day(dt.date(2026, 12, 24))


def test_first_trading_day_of_month_skips_holidays() -> None:
    """Jun 1 2026 is a Monday and a full NSE trading day."""
    assert is_first_trading_day_of_month(dt.date(2026, 6, 1))
    assert not is_first_trading_day_of_month(dt.date(2026, 6, 2))


def test_first_trading_day_handles_republic_day() -> None:
    """Jan 26 2026 is Republic Day (closed). Jan 1 was also closed
    (per US holiday list… but NSE has Republic Day in Jan, not New
    Year's). Either way, Jan 2 2026 (Fri) is the first trading day."""
    # Jan 1 2026 is a Thu — NSE does NOT close for New Year's, so it IS
    # the first trading day of January.
    assert is_first_trading_day_of_month(dt.date(2026, 1, 1))
    assert not is_first_trading_day_of_month(dt.date(2026, 1, 2))


def test_first_trading_day_handles_weekend_anchor() -> None:
    """Feb 1 2026 is Sunday. Feb 2 (Mon) is the first trading day."""
    assert not is_first_trading_day_of_month(dt.date(2026, 2, 1))
    assert is_first_trading_day_of_month(dt.date(2026, 2, 2))


def test_previous_trading_day_after_republic_day_long_weekend() -> None:
    """Republic Day 2026 = Mon Jan 26 (closed). Sat Jan 24 + Sun Jan 25
    are weekend. Previous trading day relative to Tue Jan 27 is Fri Jan 23."""
    assert previous_trading_day(dt.date(2026, 1, 27)) == dt.date(2026, 1, 23)


def test_next_trading_day_skips_holiday() -> None:
    """Next trading day after Mon Jan 26 2026 (Republic Day) is Tue."""
    assert next_trading_day(dt.date(2026, 1, 26)) == dt.date(2026, 1, 27)


def test_is_us_equity_trading_day_aliases_to_nse() -> None:
    """The old name still works as an alias for backward compat."""
    assert is_us_equity_trading_day(dt.date(2026, 5, 19))
    assert not is_us_equity_trading_day(dt.date(2026, 1, 26))


def test_horizon_exceeded_raises() -> None:
    beyond = CALENDAR_HORIZON_END + dt.timedelta(days=1)
    with pytest.raises(CalendarHorizonExceeded):
        is_nse_trading_day(beyond)
