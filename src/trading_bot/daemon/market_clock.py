"""Market-clock helpers — RTH detection for the equity lane.

The freshness kill switch will fire if the equity watermark falls more
than ``policy/data_freshness.lock`` seconds behind wall clock. Outside
regular trading hours (RTH), Alpaca bars don't update — that's not a
data outage, it's the market being closed. The daemon must treat those
two cases differently.

Conservatively: we treat RTH as 09:30–16:00 America/New_York, Monday
through Friday, ignoring US market holidays. Holidays produce a false
positive ingest run that returns no fresh bars; the daemon logs but
doesn't fire the kill switch (see jobs.job_market_data_ingest).

Crypto runs 24/7; its lane is governed by a separate watermark and a
different kill threshold. This module only owns equity RTH.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

RTH_OPEN = dt.time(9, 30)
RTH_CLOSE = dt.time(16, 0)


def is_equity_rth(now: dt.datetime | None = None) -> bool:
    """True iff ``now`` (default = wall clock) falls within US equity
    regular trading hours."""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(NY_TZ)
    if local.weekday() >= 5:           # Sat / Sun
        return False
    t = local.time()
    return RTH_OPEN <= t < RTH_CLOSE


def next_rth_open(now: dt.datetime | None = None) -> dt.datetime:
    """Next America/New_York 09:30 weekday boundary, UTC."""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(NY_TZ)
    candidate = local.replace(hour=9, minute=30, second=0, microsecond=0)
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
    "NY_TZ", "RTH_CLOSE", "RTH_OPEN",
    "is_equity_rth", "next_rth_open", "seconds_until_rth_open",
]
