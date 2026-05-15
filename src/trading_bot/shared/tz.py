"""Operator timezone display helpers.

The ledger + every hash chain stores timestamps in UTC. The operator
reads them in **America/New_York (EST/EDT)**. This module is the single
place where UTC → operator-local conversion happens; anything that
renders to the operator (dashboard, CLI, digest, email) goes through
``format_et`` / ``now_et_str``.

Why a module not a magic format string: when DST changes, when the
operator moves abroad, or when we want to honor a `TRADING_BOT_TZ` env
override, we change *here only* and nothing else.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

OPERATOR_TZ_ENV = "TRADING_BOT_TZ"
DEFAULT_TZ = "America/New_York"


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


def format_et(
    ts: dt.datetime | str | None,
    *,
    fmt: str = "%Y-%m-%d %H:%M:%S %Z",
    fallback: str = "—",
) -> str:
    out = to_operator(ts)
    if out is None:
        return fallback
    return out.strftime(fmt)


def now_et_str(fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    return dt.datetime.now(operator_tz()).strftime(fmt)


__all__ = [
    "DEFAULT_TZ", "OPERATOR_TZ_ENV",
    "format_et", "now_et_str", "operator_tz", "to_operator",
]
