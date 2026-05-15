"""Daemon-level session-start equity helper.

The risk kernel's intraday checks (``check_daily_drawdown`` /
``check_intraday_pnl_floor``) need ``equity_at_session_start`` — the
equity at the most recent US equity-session open. Without it those
checks always pass.

We reconstruct the anchor from the existing ``account_snapshot`` table
(written every 5 min during RTH by ``job_account_snapshot``). The
first snapshot whose timestamp falls in the current session is the
anchor; before the first snapshot of the day we degrade safely by
returning the caller-supplied current equity (which yields a zero DD
on the first risk check after open — same behaviour as before, but
documented).

The session anchor for the equity lane is America/New_York 09:30. For
weekends and holidays we fall back to the most recent prior session
open, which keeps Saturday/Sunday crypto strategies honest about their
drawdown without forcing the daemon to know the holiday calendar.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from trading_bot.daemon.market_clock import NY_TZ, RTH_OPEN


def _current_session_anchor_utc(now: dt.datetime | None = None) -> dt.datetime:
    """Return the UTC timestamp of the most recent US-equity 09:30 ET
    boundary that is <= ``now`` (default: wall clock). On weekends or
    before today's 09:30, walks back to the previous weekday's 09:30 so
    that an anchor always exists."""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(NY_TZ)
    candidate = local.replace(
        hour=RTH_OPEN.hour,
        minute=RTH_OPEN.minute,
        second=0,
        microsecond=0,
    )
    if local.time() < RTH_OPEN:
        candidate = candidate - dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate - dt.timedelta(days=1)
    return candidate.astimezone(dt.timezone.utc)


def session_start_equity(
    conn: sqlite3.Connection,
    *,
    fallback_equity: float,
    now: dt.datetime | None = None,
) -> float:
    """Equity at the most recent US session open, read from
    ``account_snapshot``. Falls back to ``fallback_equity`` when no
    snapshot exists in the current session yet (e.g. daemon just
    booted, or strategy_runner fires before the first account
    snapshot of the day).

    This function is read-only; it never writes to the ledger.
    """
    anchor = _current_session_anchor_utc(now)
    try:
        cur = conn.execute(
            "SELECT equity FROM account_snapshot "
            "WHERE snapshot_ts >= ? "
            "ORDER BY snapshot_ts ASC LIMIT 1",
            (anchor.isoformat(),),
        )
    except sqlite3.OperationalError:
        # account_snapshot table not yet created (fresh ledger).
        return float(fallback_equity)
    row = cur.fetchone()
    if row is None or row[0] is None:
        return float(fallback_equity)
    return float(row[0])


__all__ = ["session_start_equity"]
