"""Market-clock helpers — RTH detection."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from trading_bot.daemon.market_clock import (
    is_equity_rth, next_rth_open, seconds_until_rth_open,
)

NY = ZoneInfo("America/New_York")


def _ny(year, month, day, hour, minute) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(dt.timezone.utc)


def test_rth_open_at_0930_ny_weekday():
    assert is_equity_rth(_ny(2026, 5, 14, 9, 30))    # Thursday
    assert is_equity_rth(_ny(2026, 5, 14, 12, 0))
    assert is_equity_rth(_ny(2026, 5, 14, 15, 59))


def test_rth_closed_at_close_and_after():
    assert not is_equity_rth(_ny(2026, 5, 14, 16, 0))
    assert not is_equity_rth(_ny(2026, 5, 14, 18, 0))
    assert not is_equity_rth(_ny(2026, 5, 14, 9, 29))


def test_rth_closed_on_weekend():
    # Saturday 2026-05-16, 12:00 ET — should be closed.
    assert not is_equity_rth(_ny(2026, 5, 16, 12, 0))
    assert not is_equity_rth(_ny(2026, 5, 17, 12, 0))   # Sunday


def test_next_rth_open_from_weekday_after_close():
    # Friday 18:00 ET → next Monday 09:30 ET
    nxt = next_rth_open(_ny(2026, 5, 15, 18, 0))
    assert nxt.astimezone(NY).weekday() == 0           # Monday
    assert nxt.astimezone(NY).hour == 9
    assert nxt.astimezone(NY).minute == 30


def test_seconds_until_open_zero_during_rth():
    assert seconds_until_rth_open(_ny(2026, 5, 14, 12, 0)) == 0


def test_seconds_until_open_positive_outside_rth():
    s = seconds_until_rth_open(_ny(2026, 5, 14, 8, 0))    # before open
    assert s > 0 and s < 3600 * 3                          # less than 3h to 09:30
