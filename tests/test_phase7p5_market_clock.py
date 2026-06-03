"""Market-clock helpers — RTH detection for NSE equity (09:15–15:30 IST)."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from trading_bot.daemon.market_clock import (
    is_equity_rth, is_pre_open, next_rth_open, seconds_until_rth_open,
)

IST = ZoneInfo("Asia/Kolkata")


def _ist(year, month, day, hour, minute) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(dt.timezone.utc)


def test_rth_open_at_0915_ist_weekday():
    # Thursday 2026-05-14.
    assert is_equity_rth(_ist(2026, 5, 14, 9, 15))
    assert is_equity_rth(_ist(2026, 5, 14, 12, 0))
    assert is_equity_rth(_ist(2026, 5, 14, 15, 29))


def test_rth_closed_at_close_and_after():
    assert not is_equity_rth(_ist(2026, 5, 14, 15, 30))   # close boundary
    assert not is_equity_rth(_ist(2026, 5, 14, 18, 0))
    assert not is_equity_rth(_ist(2026, 5, 14, 9, 14))    # one min pre-open


def test_pre_open_window_detected():
    # 09:00–09:15 IST is pre-open call auction, NOT RTH.
    assert is_pre_open(_ist(2026, 5, 14, 9, 0))
    assert is_pre_open(_ist(2026, 5, 14, 9, 14))
    assert not is_pre_open(_ist(2026, 5, 14, 9, 15))      # RTH started
    assert not is_pre_open(_ist(2026, 5, 14, 8, 59))


def test_rth_closed_on_weekend():
    # Saturday 2026-05-16 IST.
    assert not is_equity_rth(_ist(2026, 5, 16, 12, 0))
    assert not is_equity_rth(_ist(2026, 5, 17, 12, 0))    # Sunday


def test_next_rth_open_from_weekday_after_close():
    """Fri 16:00 IST (after close) → next Mon 09:15 IST."""
    nxt = next_rth_open(_ist(2026, 5, 15, 16, 0))
    nxt_ist = nxt.astimezone(IST)
    assert nxt_ist.weekday() == 0                          # Monday
    assert nxt_ist.hour == 9
    assert nxt_ist.minute == 15


def test_seconds_until_open_zero_during_rth():
    assert seconds_until_rth_open(_ist(2026, 5, 14, 12, 0)) == 0


def test_seconds_until_open_positive_outside_rth():
    # 08:00 IST → less than ~1h 15m to 09:15.
    s = seconds_until_rth_open(_ist(2026, 5, 14, 8, 0))
    assert 0 < s < 3600 * 2
