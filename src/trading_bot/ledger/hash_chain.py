"""Hash-chain compute + verification for the v4 ledger.

Every event-table row carries ``prev_hash`` (previous row's
``this_hash``) and ``this_hash`` (computed below). The chain is verified:

  - at kernel startup (Phase 2 wires the boot path)
  - nightly by a CLI (``tools/verify_ledger.py``)
  - by the test suite on every insert

If verification fails for any row, the kernel halts new entries and an
incident row is appended to ``reconciliation_proof`` (Phase 2 wires
that). For Phase 1 we just expose ``verify_chain`` so tests can call it.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Iterable, Mapping

from trading_bot.ledger.canonical import canonical_json

# Sixty-four zero hex chars — the prev_hash for the very first row in a
# hash-chained table.
GENESIS_PREV_HASH = "0" * 64


def compute_this_hash(prev_hash: str, row: Mapping[str, object]) -> str:
    """Return the sha256 hex digest used for ``this_hash``."""
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(canonical_json(row))
    return h.hexdigest()


def last_hash(conn: sqlite3.Connection, table: str) -> str:
    """Return the ``this_hash`` of the most recently inserted row in ``table``,
    or ``GENESIS_PREV_HASH`` if empty.
    """
    cur = conn.cursor()
    cur.execute(
        f"SELECT this_hash FROM {table} ORDER BY ledger_seq DESC LIMIT 1"
    )
    row = cur.fetchone()
    return row[0] if row else GENESIS_PREV_HASH


class HashChainBroken(Exception):
    """Raised when a row's stored hash does not match its recomputed hash."""

    def __init__(self, table: str, ledger_seq: int, expected: str, actual: str):
        self.table = table
        self.ledger_seq = ledger_seq
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"hash chain broken in {table} at ledger_seq={ledger_seq}: "
            f"stored={actual[:16]}... recomputed={expected[:16]}..."
        )


def verify_chain(conn: sqlite3.Connection, table: str) -> int:
    """Walk every row in ``table`` in ``ledger_seq`` order; recompute each
    ``this_hash``; raise ``HashChainBroken`` on the first mismatch.

    Returns the number of rows verified on success.
    """
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} ORDER BY ledger_seq ASC")
    columns = [c[0] for c in cur.description]
    prev = GENESIS_PREV_HASH
    n = 0
    for row_tuple in cur:
        row = dict(zip(columns, row_tuple))
        stored = row.get("this_hash")
        stored_prev = row.get("prev_hash")
        if stored_prev != prev:
            raise HashChainBroken(table, row["ledger_seq"], prev, stored_prev)
        recomputed = compute_this_hash(prev, row)
        if recomputed != stored:
            raise HashChainBroken(table, row["ledger_seq"], recomputed, stored)
        prev = stored
        n += 1
    return n


def verify_all_chained(conn: sqlite3.Connection) -> dict[str, int]:
    """Verify every hash-chained table. Returns ``{table: row_count}`` on
    success; raises ``HashChainBroken`` on the first broken table.
    """
    from trading_bot.ledger.schema import HASH_CHAINED_TABLES
    return {t: verify_chain(conn, t) for t in HASH_CHAINED_TABLES}


__all__ = [
    "GENESIS_PREV_HASH",
    "HashChainBroken",
    "compute_this_hash",
    "last_hash",
    "verify_all_chained",
    "verify_chain",
]
