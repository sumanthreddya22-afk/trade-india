"""Operator timezone display helpers.

The ledger + every hash chain stores timestamps in UTC. The operator
reads them in **Asia/Kolkata (IST, UTC+5:30)**. This module is the
single place where UTC → operator-local conversion happens; anything
that renders to the operator (dashboard, CLI, digest, email) goes
through ``format_ist`` / ``now_ist_str``.

India does not observe daylight saving time — IST is a fixed UTC+5:30
offset year-round. NSE/BSE session: 09:15–15:30 IST, Monday–Friday.

Why a module not a magic format string: if the operator moves timezone
or we want to honour a ``TRADING_BOT_TZ`` env override, we change
*here only* and nothing else.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

OPERATOR_TZ_ENV = "TRADING_BOT_TZ"
DEFAULT_TZ = "Asia/Kolkata"


def operator_tz() -> ZoneInfo:
    name = os.environ.get(OPERATOR_TZ_ENV, "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def to_operator(ts: dt.datetime | str | None) -> dt.datetime | None:
    """Convert any UTC-or-naive datetime (or ISO string) to operator tz."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(operator_tz())


def format_ist(
    ts: dt.datetime | str | None,
    *,
    fmt: str = "%Y-%m-%d %H:%M:%S %Z",
    fallback: str = "—",
) -> str:
    out = to_operator(ts)
    if out is None:
        return fallback
    return out.strftime(fmt)


# Backward-compat alias (older callsites used format_et).
format_et = format_ist


def now_ist_str(fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    return dt.datetime.now(operator_tz()).strftime(fmt)


# Backward-compat alias.
now_et_str = now_ist_str


__all__ = [
    "DEFAULT_TZ", "OPERATOR_TZ_ENV",
    "format_et", "format_ist", "now_et_str", "now_ist_str",
    "operator_tz", "to_operator",
]
