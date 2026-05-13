"""Phase 3 — data freshness watermarks."""
from __future__ import annotations

import datetime as dt

from trading_bot.ingest import (
    Watermark, check_lane_freshness, latest_watermark_for_lane,
    read_watermark, write_watermark,
)


_FRESHNESS_LOCK = {
    "per_lane_max_age_seconds": {
        "equity": 300, "crypto": 60, "option": 60,
    }
}


def test_write_then_read(ledger_conn) -> None:
    ts = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    write_watermark(ledger_conn, source_id="alpaca", lane="equity",
                    event_ts=ts)
    wm = read_watermark(ledger_conn, source_id="alpaca", lane="equity")
    assert wm is not None
    assert wm.source_id == "alpaca"
    assert wm.lane == "equity"


def test_write_is_upsert(ledger_conn) -> None:
    t1 = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 5, 13, 12, 5, 0, tzinfo=dt.timezone.utc)
    write_watermark(ledger_conn, source_id="alpaca", lane="equity",
                    event_ts=t1)
    write_watermark(ledger_conn, source_id="alpaca", lane="equity",
                    event_ts=t2)
    wm = read_watermark(ledger_conn, source_id="alpaca", lane="equity")
    assert wm.last_event_ts == t2
    cur = ledger_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM data_watermark")
    assert cur.fetchone()[0] == 1


def test_latest_watermark_for_lane_returns_freshest(ledger_conn) -> None:
    t1 = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 5, 13, 12, 5, 0, tzinfo=dt.timezone.utc)
    write_watermark(ledger_conn, source_id="alpaca", lane="equity",
                    event_ts=t1)
    write_watermark(ledger_conn, source_id="polygon", lane="equity",
                    event_ts=t2)
    wm = latest_watermark_for_lane(ledger_conn, "equity")
    assert wm.source_id == "polygon"
    assert wm.last_event_ts == t2


def test_check_freshness_pass(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    fresh = now - dt.timedelta(seconds=10)
    write_watermark(ledger_conn, source_id="alpaca", lane="equity",
                    event_ts=fresh)
    d = check_lane_freshness(
        ledger_conn, lane="equity",
        data_freshness_lock=_FRESHNESS_LOCK, now=now,
    )
    assert d.verdict == "accept"


def test_check_freshness_stale(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    stale = now - dt.timedelta(seconds=400)
    write_watermark(ledger_conn, source_id="alpaca", lane="equity",
                    event_ts=stale)
    d = check_lane_freshness(
        ledger_conn, lane="equity",
        data_freshness_lock=_FRESHNESS_LOCK, now=now,
    )
    assert d.verdict == "halt"
    assert "stale" in d.reason


def test_check_freshness_missing_watermark_halts(ledger_conn) -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    d = check_lane_freshness(
        ledger_conn, lane="equity",
        data_freshness_lock=_FRESHNESS_LOCK, now=now,
    )
    assert d.verdict == "halt"
    assert "no_watermark" in d.reason


def test_check_freshness_unknown_lane_halts(ledger_conn) -> None:
    d = check_lane_freshness(
        ledger_conn, lane="forex",
        data_freshness_lock=_FRESHNESS_LOCK,
    )
    assert d.verdict == "halt"
    assert "no_threshold_for_lane" in d.reason
