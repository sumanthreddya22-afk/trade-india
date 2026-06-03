"""Market-clock helpers — RTH detection for the NSE equity lane.

NSE/BSE regular trading hours (RTH): 09:15–15:30 IST, Monday–Friday.
Pre-open session: 09:00–09:15 IST (call auction; treated as pre-market,
not RTH for order purposes).

India does NOT observe daylight saving time — IST is a fixed UTC+5:30
offset year-round. This simplifies all time arithmetic vs the old US path.

The freshness kill switch fires if the equity watermark falls more than
``policy/data_freshness.lock`` seconds behind wall clock. Outside RTH,
Kite Connect / NSE bars don't update — that's the market being closed,
not a data outage.

Crypto (BTC/ETH INR pairs) runs 24/7 on Indian exchanges; its lane is
governed by a separate watermark and a different kill threshold. This
module only owns equity RTH.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

IST_TZ = ZoneInfo("Asia/Kolkata")

# Backward-compat alias so any code referencing NY_TZ still imports cleanly.
NY_TZ = IST_TZ

# NSE regular trading session
RTH_OPEN = dt.time(9, 15)
RTH_CLOSE = dt.time(15, 30)

# Pre-open call auction window (orders accepted but not executed)
PRE_OPEN_START = dt.time(9, 0)
PRE_OPEN_END = dt.time(9, 15)


def is_equity_rth(now: dt.datetime | None = None) -> bool:
    """True iff ``now`` (default = wall clock) falls within NSE equity
    regular trading hours (09:15–15:30 IST, Mon–Fri)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(IST_TZ)
    if local.weekday() >= 5:           # Sat / Sun
        return False
    t = local.time()
    return RTH_OPEN <= t < RTH_CLOSE


def is_pre_open(now: dt.datetime | None = None) -> bool:
    """True iff ``now`` falls within NSE pre-open call auction
    (09:00–09:15 IST, Mon–Fri)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(IST_TZ)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return PRE_OPEN_START <= t < PRE_OPEN_END


def next_rth_open(now: dt.datetime | None = None) -> dt.datetime:
    """Next IST 09:15 weekday boundary, returned as UTC datetime."""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(IST_TZ)
    candidate = local.replace(hour=9, minute=15, second=0, microsecond=0)
    # If we're already past open today (or it's a weekend), move to next day.
    if local.time() >= RTH_OPEN or local.weekday() >= 5:
        candidate = candidate + dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + dt.timedelta(days=1)
    return candidate.astimezone(dt.timezone.utc)


def seconds_until_rth_open(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    if is_equity_rth(now):
        return 0
    return int((next_rth_open(now) - now).total_seconds())


__all__ = [
    "IST_TZ", "NY_TZ",
    "PRE_OPEN_END", "PRE_OPEN_START",
    "RTH_CLOSE", "RTH_OPEN",
    "is_equity_rth", "is_pre_open",
    "next_rth_open", "seconds_until_rth_open",
]
