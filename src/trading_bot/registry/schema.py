"""DDL for the L4 strategy registry.

Plan v4 §3 + §13. Three tables, all in ``ledger.db``:

  - strategy_version   : append-only by (strategy_id, strategy_ver); new
                          version means new row.
  - validation_artifact: append-only, hash-chained.
  - promotion_packet   : append-only, hash-chained, Tier-3 evidence
                          bundle.

Triggers raise on UPDATE / DELETE for all three.
"""
from __future__ import annotations

DDL_STRATEGY_VERSION = """
CREATE TABLE IF NOT EXISTS strategy_version (
    strategy_id            TEXT NOT NULL,
    strategy_ver           INTEGER NOT NULL,
    code_hash              TEXT NOT NULL,
    config_hash            TEXT NOT NULL,
    thesis_id              TEXT NOT NULL,
    hypothesis_id          TEXT NOT NULL,
    validation_artifact_id TEXT,
    lane                   TEXT NOT NULL,
    status                 TEXT NOT NULL,
    expiry_date            TEXT,
    owner                  TEXT NOT NULL,
    created_ts             TEXT NOT NULL,
    PRIMARY KEY (strategy_id, strategy_ver)
);
"""

DDL_VALIDATION_ARTIFACT = """
CREATE TABLE IF NOT EXISTS validation_artifact (
    ledger_seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id      TEXT NOT NULL UNIQUE,
    strategy_id      TEXT NOT NULL,
    strategy_ver     INTEGER NOT NULL,
    tier             TEXT NOT NULL,
    produced_ts      TEXT NOT NULL,
    code_hash        TEXT NOT NULL,
    config_hash      TEXT NOT NULL,
    metrics_json     TEXT NOT NULL,
    lens             TEXT NOT NULL,
    pass             INTEGER NOT NULL,
    failure_reasons  TEXT,
    prev_hash        TEXT NOT NULL,
    this_hash        TEXT NOT NULL
);
"""

DDL_PROMOTION_PACKET = """
CREATE TABLE IF NOT EXISTS promotion_packet (
    ledger_seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id              TEXT NOT NULL UNIQUE,
    strategy_id            TEXT NOT NULL,
    strategy_ver           INTEGER NOT NULL,
    target_tier            TEXT NOT NULL,
    code_hash              TEXT NOT NULL,
    config_hash            TEXT NOT NULL,
    validation_artifact_id TEXT NOT NULL,
    paper_scorecard_id     TEXT,
    risk_review_id         TEXT,
    known_failure_modes_json TEXT,
    expiry_date            TEXT NOT NULL,
    operator_signed        INTEGER NOT NULL,
    created_ts             TEXT NOT NULL,
    prev_hash              TEXT NOT NULL,
    this_hash              TEXT NOT NULL
);
"""

_APPEND_ONLY = ("strategy_version", "validation_artifact", "promotion_packet")


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


REGISTRY_DDL = [DDL_STRATEGY_VERSION, DDL_VALIDATION_ARTIFACT, DDL_PROMOTION_PACKET]
for _t in _APPEND_ONLY:
    REGISTRY_DDL.extend(_trigger_ddl(_t))

REGISTRY_DDL.extend([
    "CREATE INDEX IF NOT EXISTS idx_sv_strategy ON strategy_version(strategy_id);",
    "CREATE INDEX IF NOT EXISTS idx_va_strategy_tier ON validation_artifact(strategy_id, tier);",
    "CREATE INDEX IF NOT EXISTS idx_pp_strategy ON promotion_packet(strategy_id);",
])


def ensure_registry_tables(conn) -> None:
    cur = conn.cursor()
    for stmt in REGISTRY_DDL:
        cur.execute(stmt)
    conn.commit()


__all__ = ["REGISTRY_DDL", "ensure_registry_tables"]
