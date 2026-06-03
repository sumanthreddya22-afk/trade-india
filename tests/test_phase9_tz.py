"""IST display helpers (operator timezone = Asia/Kolkata)."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from trading_bot.shared.tz import (
    DEFAULT_TZ, format_et, format_ist, now_et_str, now_ist_str,
    operator_tz, to_operator,
)


def test_default_tz_is_asia_kolkata():
    tz = operator_tz()
    assert str(tz) == DEFAULT_TZ
    assert DEFAULT_TZ == "Asia/Kolkata"


def test_to_operator_converts_utc():
    # 18:00 UTC → 23:30 IST (IST = UTC+5:30, no DST).
    utc = dt.datetime(2026, 1, 15, 18, 0, 0, tzinfo=dt.timezone.utc)
    ist = to_operator(utc)
    assert ist.tzinfo is not None
    assert ist.hour == 23
    assert ist.minute == 30


def test_to_operator_handles_string():
    # 14:00 UTC → 19:30 IST.
    ist = to_operator("2026-07-15T14:00:00+00:00")
    assert ist is not None
    assert ist.hour == 19
    assert ist.minute == 30


def test_format_ist_default_format():
    s = format_ist("2026-01-15T18:30:45+00:00")
    # 18:30:45 UTC → 00:00:45 IST next day.
    assert "2026-01-16" in s
    assert "00:00:45" in s
    # IST tzname is "IST".
    assert "IST" in s or "+0530" in s or "+05:30" in s


def test_format_et_alias_works():
    """Backward-compat: old callers using format_et still work but now
    render IST, not ET."""
    s = format_et("2026-01-15T18:30:45+00:00")
    assert "IST" in s or "+0530" in s or "+05:30" in s


def test_now_ist_str_is_recent():
    s = now_ist_str()
    assert str(dt.date.today().year) in s


def test_now_et_str_alias_works():
    s = now_et_str()
    assert str(dt.date.today().year) in s


def test_format_ist_none_returns_fallback():
    assert format_ist(None) == "—"
    assert format_ist(None, fallback="n/a") == "n/a"
