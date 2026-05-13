"""DDL for the mutation engine.

Plan v4 §8: every candidate variant gets one entry in ``mutation_log``
at proposal time; the backtest outcome (raw p-value, BH-FDR adjusted
p-value, survived flag) is appended to ``mutation_outcome``. Both
tables are hash-chained, append-only.
"""
from __future__ import annotations

DDL_MUTATION_LOG = """
CREATE TABLE IF NOT EXISTS mutation_log (
    ledger_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id      TEXT NOT NULL UNIQUE,
    thesis_id         TEXT NOT NULL,
    family            TEXT NOT NULL,
    mutation_id       TEXT NOT NULL,
    variant_value     TEXT NOT NULL,
    cycle_id          TEXT NOT NULL,
    proposed_ts       TEXT NOT NULL,
    hypothesis_hash   TEXT NOT NULL,
    rationale         TEXT,
    proposer          TEXT NOT NULL,
    prev_hash         TEXT NOT NULL,
    this_hash         TEXT NOT NULL
);
"""

DDL_MUTATION_OUTCOME = """
CREATE TABLE IF NOT EXISTS mutation_outcome (
    ledger_seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id      TEXT NOT NULL,
    outcome_ts        TEXT NOT NULL,
    raw_p_value       REAL NOT NULL,
    adjusted_p_value  REAL,
    survived          INTEGER,
    sanity_checks     TEXT,
    prev_hash         TEXT NOT NULL,
    this_hash         TEXT NOT NULL
);
"""

_APPEND_ONLY = ("mutation_log", "mutation_outcome")


def _trigger_ddl(table: str) -> list[str]:
    return [
        f"""
        CREATE TRIGGER IF NOT EXISTS no_update_{table}
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only; UPDATE is forbidden by v4 §5');
        END;
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS no_delete_{table}
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only; DELETE is forbidden by v4 §5');
        END;
        """,
    ]


MUTATION_DDL = [DDL_MUTATION_LOG, DDL_MUTATION_OUTCOME]
for _t in _APPEND_ONLY:
    MUTATION_DDL.extend(_trigger_ddl(_t))

MUTATION_DDL.extend([
    "CREATE INDEX IF NOT EXISTS idx_ml_cycle ON mutation_log(cycle_id);",
    "CREATE INDEX IF NOT EXISTS idx_mo_candidate ON mutation_outcome(candidate_id);",
])


def ensure_mutation_tables(conn) -> None:
    cur = conn.cursor()
    for stmt in MUTATION_DDL:
        cur.execute(stmt)
    conn.commit()


__all__ = ["MUTATION_DDL", "ensure_mutation_tables"]
