"""DDL for the failure_memory table.

Plan v4 §8: "Rejected candidates stored with hypothesis_hash + reason;
same hypothesis_hash auto-rejected for 90 days unless thesis changes."

Hash-chained append-only event log in ``ledger.db``.
"""
from __future__ import annotations

DDL_FAILURE_MEMORY = """
CREATE TABLE IF NOT EXISTS failure_memory (
    ledger_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_hash   TEXT NOT NULL,
    rejected_ts       TEXT NOT NULL,
    reason            TEXT NOT NULL,
    strategy_id       TEXT,
    tier              TEXT,
    prev_hash         TEXT NOT NULL,
    this_hash         TEXT NOT NULL
);
"""

DDL_FAILURE_MEMORY_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_fm_hypothesis ON failure_memory(hypothesis_hash);",
    "CREATE INDEX IF NOT EXISTS idx_fm_ts ON failure_memory(rejected_ts);",
]

DDL_FAILURE_MEMORY_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS no_update_failure_memory
    BEFORE UPDATE ON failure_memory
    BEGIN
        SELECT RAISE(ABORT, 'failure_memory is append-only; UPDATE is forbidden by v4 §5');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS no_delete_failure_memory
    BEFORE DELETE ON failure_memory
    BEGIN
        SELECT RAISE(ABORT, 'failure_memory is append-only; DELETE is forbidden by v4 §5');
    END;
    """,
]


def ensure_failure_memory(conn) -> None:
    cur = conn.cursor()
    cur.execute(DDL_FAILURE_MEMORY)
    for idx in DDL_FAILURE_MEMORY_INDICES:
        cur.execute(idx)
    for trig in DDL_FAILURE_MEMORY_TRIGGERS:
        cur.execute(trig)
    conn.commit()


__all__ = [
    "DDL_FAILURE_MEMORY",
    "DDL_FAILURE_MEMORY_INDICES",
    "DDL_FAILURE_MEMORY_TRIGGERS",
    "ensure_failure_memory",
]
