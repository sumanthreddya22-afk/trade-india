"""Off-host append-only mirror.

Plan v4 §5 immutability defense #3: "every event is also written, in
order, to a separate file (or remote object store with object-lock); the
chain is re-verified there nightly."

Phase 1 implementation: a sibling SQLite DB at ``data/ledger/mirror.db``.
Operator can later swap the mirror path to a different volume or to an
S3-with-object-lock mount — the writer API stays the same.

Usage pattern (Phase 2+ wires this into every ledger write):

    with ledger_conn:
        ledger_seq = append_state_event(ledger_conn, ...)
        mirror_event(mirror_conn, "order_state_event", ledger_conn, ledger_seq)

The mirror copy carries the same ``prev_hash`` and ``this_hash`` as the
canonical row, so verifying the mirror's chain produces identical hashes
to the canonical chain. Divergence at any row is a tamper indicator.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from trading_bot.ledger.schema import HASH_CHAINED_TABLES, create_ledger


def init_mirror(mirror_path: Path) -> sqlite3.Connection:
    """Create the mirror DB (same schema as the canonical ledger) and
    return a writer connection."""
    mirror_path = Path(mirror_path)
    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(mirror_path), isolation_level=None, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    create_ledger(conn)
    return conn


def mirror_event(
    mirror_conn: sqlite3.Connection,
    table: str,
    src_conn: sqlite3.Connection,
    ledger_seq: int,
) -> None:
    """Copy one row from ``src_conn``'s ``table`` (identified by
    ``ledger_seq``) into ``mirror_conn``'s same table.

    Idempotent on ``ledger_seq`` PK conflict — re-mirroring an already
    mirrored row is a no-op rather than an error, which is the behaviour
    we want when a writer crashed mid-mirror and restarts.
    """
    if table not in HASH_CHAINED_TABLES and table != "order_master":
        raise ValueError(f"mirror_event: unsupported table {table!r}")
    cur = src_conn.cursor()
    cur.execute(f"SELECT * FROM {table} WHERE ledger_seq = ?", (ledger_seq,))
    row = cur.fetchone()
    if row is None:
        # order_master has no ledger_seq column — try PK lookup by
        # client_order_id instead. Caller should use mirror_order_master.
        raise ValueError(
            f"mirror_event: no row in {table} at ledger_seq={ledger_seq}"
        )
    columns = [c[0] for c in cur.description]
    placeholders = ",".join("?" for _ in columns)
    mirror_conn.execute(
        f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        row,
    )


def mirror_order_master(
    mirror_conn: sqlite3.Connection,
    src_conn: sqlite3.Connection,
    order_uid: str,
) -> None:
    cur = src_conn.cursor()
    cur.execute("SELECT * FROM order_master WHERE order_uid = ?", (order_uid,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"mirror_order_master: order_uid={order_uid} not found")
    columns = [c[0] for c in cur.description]
    placeholders = ",".join("?" for _ in columns)
    mirror_conn.execute(
        f"INSERT OR IGNORE INTO order_master ({','.join(columns)}) VALUES ({placeholders})",
        row,
    )


__all__ = ["init_mirror", "mirror_event", "mirror_order_master"]
