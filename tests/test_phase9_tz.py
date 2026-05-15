"""EST display helpers."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from trading_bot.shared.tz import (
    DEFAULT_TZ, format_et, now_et_str, operator_tz, to_operator,
)


def test_default_tz_is_eastern():
    tz = operator_tz()
    assert str(tz) == DEFAULT_TZ


def test_to_operator_converts_utc():
    utc = dt.datetime(2026, 1, 15, 18, 0, 0, tzinfo=dt.timezone.utc)
    et = to_operator(utc)
    assert et.tzinfo is not None
    assert et.hour == 13   # Jan = EST (UTC-5)


def test_to_operator_handles_string():
    et = to_operator("2026-07-15T14:00:00+00:00")
    assert et is not None
    # Jul = EDT (UTC-4) → 10am ET
    assert et.hour == 10


def test_format_et_default_format():
    s = format_et("2026-01-15T18:30:45+00:00")
    assert "2026-01-15" in s
    assert "13:30:45" in s
    assert "EST" in s or "EDT" in s or "-05" in s or "-04" in s


def test_now_et_str_is_recent():
    s = now_et_str()
    assert str(dt.date.today().year) in s


def test_format_et_none_returns_fallback():
    assert format_et(None) == "—"
    assert format_et(None, fallback="n/a") == "n/a"
