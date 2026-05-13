"""Data freshness watermarks.

Plan v4 §6 + §1B: the kernel reads the latest watermark per (source_id,
lane) before any intent; if the lane's freshness exceeds the threshold
in ``policy/data_freshness.lock``, intents for that lane are blocked.

Watermarks are mutable by design — every new tick updates the latest.
This is the only mutable table in the ledger DB; everything else is
append-only.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Optional

from trading_bot.ingest.schema import ensure_ingest_tables
from trading_bot.risk.types import RiskDecision


@dataclass(frozen=True)
class Watermark:
    source_id: str
    lane: str
    last_event_ts: dt.datetime
    last_ingest_ts: dt.datetime
    raw_payload_hash: Optional[str] = None

    @property
    def age_seconds(self) -> float:
        now = dt.datetime.now(dt.timezone.utc)
        ts = self.last_event_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return (now - ts).total_seconds()


def write_watermark(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    lane: str,
    event_ts: dt.datetime,
    ingest_ts: Optional[dt.datetime] = None,
    payload_hash: Optional[str] = None,
) -> None:
    ingest_ts = ingest_ts or dt.datetime.now(dt.timezone.utc)
    ensure_ingest_tables(conn)
    conn.execute(
        """
        INSERT INTO data_watermark
            (source_id, lane, last_event_ts, last_ingest_ts, raw_payload_hash)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_id, lane) DO UPDATE SET
            last_event_ts   = excluded.last_event_ts,
            last_ingest_ts  = excluded.last_ingest_ts,
            raw_payload_hash = excluded.raw_payload_hash
        """,
        (source_id, lane, event_ts.isoformat(), ingest_ts.isoformat(),
         payload_hash),
    )


def read_watermark(
    conn: sqlite3.Connection, *, source_id: str, lane: str,
) -> Optional[Watermark]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT source_id, lane, last_event_ts, last_ingest_ts, raw_payload_hash "
            "FROM data_watermark WHERE source_id=? AND lane=?",
            (source_id, lane),
        )
    except sqlite3.OperationalError:
        return None
    row = cur.fetchone()
    if row is None:
        return None
    return Watermark(
        source_id=row[0], lane=row[1],
        last_event_ts=dt.datetime.fromisoformat(row[2]),
        last_ingest_ts=dt.datetime.fromisoformat(row[3]),
        raw_payload_hash=row[4],
    )


def latest_watermark_for_lane(
    conn: sqlite3.Connection, lane: str,
) -> Optional[Watermark]:
    """Return the freshest (last_event_ts max) watermark for a lane,
    across all source_ids. Used by the router when it doesn't care which
    source provided the freshness."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT source_id, lane, last_event_ts, last_ingest_ts, raw_payload_hash
            FROM data_watermark
            WHERE lane = ?
            ORDER BY last_event_ts DESC
            LIMIT 1
            """,
            (lane,),
        )
    except sqlite3.OperationalError:
        return None
    row = cur.fetchone()
    if row is None:
        return None
    return Watermark(
        source_id=row[0], lane=row[1],
        last_event_ts=dt.datetime.fromisoformat(row[2]),
        last_ingest_ts=dt.datetime.fromisoformat(row[3]),
        raw_payload_hash=row[4],
    )


def check_lane_freshness(
    conn: sqlite3.Connection,
    *,
    lane: str,
    data_freshness_lock: Mapping,
    now: Optional[dt.datetime] = None,
) -> RiskDecision:
    """Return ``accept`` if the lane's freshest watermark is within its
    threshold, ``halt`` otherwise. If no watermark exists yet, returns
    ``halt`` (can't trade what we can't see).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    thresholds = data_freshness_lock.get("per_lane_max_age_seconds", {})
    threshold = thresholds.get(lane)
    if threshold is None:
        return RiskDecision.halt(f"data_freshness:no_threshold_for_lane:{lane}")

    wm = latest_watermark_for_lane(conn, lane)
    if wm is None:
        return RiskDecision.halt(f"data_freshness:no_watermark:{lane}")

    ts = wm.last_event_ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age = (now - ts).total_seconds()
    if age > threshold:
        return RiskDecision.halt(
            f"data_freshness:{lane}:stale "
            f"(age {age:.0f}s > threshold {threshold}s)"
        )
    return RiskDecision.accept()


__all__ = [
    "Watermark",
    "check_lane_freshness",
    "latest_watermark_for_lane",
    "read_watermark",
    "write_watermark",
]
