"""Daemon session-start equity helper — anchors intraday DD checks."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from trading_bot.daemon.jobs import ensure_account_snapshot_table
from trading_bot.daemon.session_state import (
    _current_session_anchor_utc,
    session_start_equity,
)


from zoneinfo import ZoneInfo
# Post India migration: anchor lives at 09:15 IST (was 09:30 ET).
# Tests use IST throughout but keep the legacy `NY` alias so the
# variable name doesn't have to be sed'd everywhere — it now points
# at Asia/Kolkata.
IST = ZoneInfo("Asia/Kolkata")
NY = IST


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    ensure_account_snapshot_table(c)
    yield c
    c.close()


def _insert_snapshot(conn: sqlite3.Connection, ts: dt.datetime, equity: float) -> None:
    conn.execute(
        "INSERT INTO account_snapshot "
        "(snapshot_ts, equity, cash, buying_power) VALUES (?, ?, ?, ?)",
        (ts.astimezone(dt.timezone.utc).isoformat(), equity, 0.0, 0.0),
    )
    conn.commit()


def test_falls_back_to_current_when_no_snapshot(conn) -> None:
    eq = session_start_equity(conn, fallback_equity=12_345.0)
    assert eq == 12_345.0


def test_returns_first_snapshot_in_session(conn) -> None:
    # Pin "now" at 14:00 IST; session anchor = today 09:15 IST.
    now = dt.datetime(2026, 5, 15, 14, 0, tzinfo=IST)
    snap_at_open = dt.datetime(2026, 5, 15, 9, 16, tzinfo=IST)
    snap_later = dt.datetime(2026, 5, 15, 13, 0, tzinfo=IST)
    _insert_snapshot(conn, snap_at_open, equity=100_000.0)
    _insert_snapshot(conn, snap_later, equity=98_500.0)
    eq = session_start_equity(conn, fallback_equity=999.0, now=now)
    assert eq == 100_000.0


def test_ignores_prior_session_snapshots(conn) -> None:
    """Snapshots from yesterday must NOT anchor today's session."""
    now = dt.datetime(2026, 5, 15, 10, 0, tzinfo=IST)
    yesterday_snap = dt.datetime(2026, 5, 14, 10, 0, tzinfo=IST)
    today_snap = dt.datetime(2026, 5, 15, 9, 16, tzinfo=IST)
    _insert_snapshot(conn, yesterday_snap, equity=90_000.0)
    _insert_snapshot(conn, today_snap, equity=100_000.0)
    eq = session_start_equity(conn, fallback_equity=999.0, now=now)
    assert eq == 100_000.0


def test_anchor_walks_back_to_friday_on_weekend(conn) -> None:
    """Sat / Sun → previous Friday 09:15 IST anchor (crypto strategies
    must still compute DD on weekends)."""
    # Saturday 2026-05-16 10:00 IST
    saturday = dt.datetime(2026, 5, 16, 10, 0, tzinfo=IST)
    anchor = _current_session_anchor_utc(saturday.astimezone(dt.timezone.utc))
    # Anchor must be Friday 2026-05-15 09:15 IST.
    anchor_ist = anchor.astimezone(IST)
    assert anchor_ist.weekday() == 4    # Friday
    assert anchor_ist.date() == dt.date(2026, 5, 15)
    assert (anchor_ist.hour, anchor_ist.minute) == (9, 15)


def test_anchor_walks_back_before_today_open(conn) -> None:
    """At 08:00 IST (pre-open), anchor should be yesterday's 09:15 —
    we have not yet entered today's session."""
    pre_open = dt.datetime(2026, 5, 15, 8, 0, tzinfo=IST)
    anchor = _current_session_anchor_utc(pre_open.astimezone(dt.timezone.utc))
    anchor_ist = anchor.astimezone(IST)
    assert anchor_ist.date() == dt.date(2026, 5, 14)
