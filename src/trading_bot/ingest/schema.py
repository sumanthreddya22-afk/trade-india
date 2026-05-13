"""DDL for the ingest layer.

Plan v4 §1B: every ingested row carries source_id, source_tier,
ingestion_ts, claimed_event_ts, verification_status, raw_payload_hash.

Phase 3 introduces:

- ``data_watermark``: per (source_id, lane) the latest data tick — mutable.
- ``corporate_action``: append-only, hash-chained event log of splits /
  dividends / mergers / spinoffs, cross-checkable across sources.
"""
from __future__ import annotations

DDL_DATA_WATERMARK = """
CREATE TABLE IF NOT EXISTS data_watermark (
    source_id        TEXT NOT NULL,
    lane             TEXT NOT NULL,                  -- equity | crypto | option
    last_event_ts    TEXT NOT NULL,
    last_ingest_ts   TEXT NOT NULL,
    raw_payload_hash TEXT,
    PRIMARY KEY (source_id, lane)
);
"""

DDL_CORPORATE_ACTION = """
CREATE TABLE IF NOT EXISTS corporate_action (
    ledger_seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts         TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    action_type      TEXT NOT NULL,                  -- split|dividend|merger|spinoff
    ex_date          TEXT NOT NULL,
    factor           REAL,
    source_id        TEXT NOT NULL,
    raw_payload_hash TEXT NOT NULL,
    prev_hash        TEXT NOT NULL,
    this_hash        TEXT NOT NULL,
    UNIQUE (symbol, action_type, ex_date, source_id)
);
"""

DDL_CORPORATE_ACTION_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS no_update_corporate_action
    BEFORE UPDATE ON corporate_action
    BEGIN
        SELECT RAISE(ABORT, 'corporate_action is append-only; UPDATE is forbidden by v4 §5');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS no_delete_corporate_action
    BEFORE DELETE ON corporate_action
    BEGIN
        SELECT RAISE(ABORT, 'corporate_action is append-only; DELETE is forbidden by v4 §5');
    END;
    """,
]

INGEST_DDL = [
    DDL_DATA_WATERMARK,
    DDL_CORPORATE_ACTION,
    *DDL_CORPORATE_ACTION_TRIGGERS,
]


def ensure_ingest_tables(conn) -> None:
    cur = conn.cursor()
    for stmt in INGEST_DDL:
        cur.execute(stmt)
    conn.commit()


__all__ = ["DDL_DATA_WATERMARK", "INGEST_DDL", "ensure_ingest_tables"]
