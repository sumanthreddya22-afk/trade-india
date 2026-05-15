"""US market calendar — full closes, half-days, first-trading-day."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.daemon.market_calendar import (
    CALENDAR_HORIZON_END,
    CalendarHorizonExceeded,
    is_early_close,
    is_first_trading_day_of_month,
    is_full_close,
    is_us_equity_trading_day,
    next_trading_day,
    previous_trading_day,
)


def test_memorial_day_2026_is_closed() -> None:
    assert is_full_close(dt.date(2026, 5, 25))
    assert not is_us_equity_trading_day(dt.date(2026, 5, 25))


def test_july_3_2026_is_observed_independence() -> None:
    """July 4 2026 is Saturday → market closed Fri Jul 3."""
    assert is_full_close(dt.date(2026, 7, 3))


def test_regular_tuesday_is_trading_day() -> None:
    assert is_us_equity_trading_day(dt.date(2026, 5, 19))


def test_weekend_is_not_trading_day() -> None:
    # 2026-05-16 is a Saturday
    assert not is_us_equity_trading_day(dt.date(2026, 5, 16))


def test_thanksgiving_friday_2026_is_half_day() -> None:
    assert is_early_close(dt.date(2026, 11, 27))
    # Early-close days are still not "full" trading days per our predicate.
    assert not is_us_equity_trading_day(dt.date(2026, 11, 27))


def test_first_trading_day_of_month_skips_holidays() -> None:
    """June 1 2026 is a Monday (full trading day) — first trading day."""
    assert is_first_trading_day_of_month(dt.date(2026, 6, 1))
    # June 2 is not.
    assert not is_first_trading_day_of_month(dt.date(2026, 6, 2))


def test_first_trading_day_handles_new_years() -> None:
    """Jan 1 2026 is Thu but a holiday. Jan 2 (Fri) is the first
    trading day. Jan 1 must NOT be flagged as the first trading day."""
    assert not is_first_trading_day_of_month(dt.date(2026, 1, 1))
    assert is_first_trading_day_of_month(dt.date(2026, 1, 2))


def test_first_trading_day_handles_weekend_anchor() -> None:
    """Feb 1 2026 is Sunday. Feb 2 (Mon) is the first trading day."""
    assert not is_first_trading_day_of_month(dt.date(2026, 2, 1))
    assert is_first_trading_day_of_month(dt.date(2026, 2, 2))


def test_previous_trading_day_after_long_weekend() -> None:
    """Memorial Day (Mon May 25 2026) is closed; previous-trading-day
    relative to Tue May 26 is Friday May 22."""
    assert previous_trading_day(dt.date(2026, 5, 26)) == dt.date(2026, 5, 22)


def test_next_trading_day_skips_holiday() -> None:
    """Next trading day after Mon May 25 2026 (Memorial Day) is Tue."""
    assert next_trading_day(dt.date(2026, 5, 25)) == dt.date(2026, 5, 26)


def test_horizon_exceeded_raises() -> None:
    beyond = CALENDAR_HORIZON_END + dt.timedelta(days=1)
    with pytest.raises(CalendarHorizonExceeded):
        is_us_equity_trading_day(beyond)
