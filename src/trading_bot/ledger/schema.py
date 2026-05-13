"""SQLite DDL for the v4 append-only, hash-chained ledger.

Plan v4 §5: "Every numeric in every report — PnL, exposure, attribution,
drawdown — derives from these tables and nothing else."

Tables:

  1. order_master            — immutable identity record per order
  2. order_state_event       — append-only state transitions (hash chained)
  3. fill_event              — append-only fills (hash chained)
  4. order_current           — derived VIEW (no stored rows)
  5. position_snapshot       — every 5 min during session + at close (hash chained)
  6. strategy_decision       — one row per kernel run (hash chained)
  7. reconciliation_proof    — nightly + at-close bot==broker proof

Immutability: every table has BEFORE-UPDATE and BEFORE-DELETE triggers that
raise an ``ABORT``. Application-level enforcement plus an off-host mirror
plus daily SHA-256 closure are the layered defenses.

Hash chain: ``this_hash = sha256(prev_hash || canonical(row))`` computed in
Python under an IMMEDIATE transaction (see ``hash_chain.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = 1
"""Bumped whenever a table or index changes. ``init_ledger.py`` refuses to
run against a DB whose stored version does not match this constant."""


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

DDL_ORDER_MASTER = """
CREATE TABLE IF NOT EXISTS order_master (
    order_uid       TEXT PRIMARY KEY,                  -- UUIDv7, generated internally
    client_order_id TEXT NOT NULL UNIQUE,              -- YYYYMMDD_<strategy>_<symbol>_<seq>
    strategy_id     TEXT NOT NULL,
    strategy_ver    INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,                     -- equity|crypto|option
    side            TEXT NOT NULL,                     -- buy|sell|sell_short|sell_to_close|buy_to_close
    qty             REAL NOT NULL,
    limit_price     REAL,
    tif             TEXT NOT NULL,                     -- day|gtc|ioc|fok
    intent_hash     TEXT NOT NULL,                     -- sha256 of canonical intent JSON
    origin          TEXT NOT NULL,                     -- strategy|risk_exit|emergency_exit|manual
    created_ts      TEXT NOT NULL
);
"""

DDL_ORDER_STATE_EVENT = """
CREATE TABLE IF NOT EXISTS order_state_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    order_uid       TEXT NOT NULL REFERENCES order_master(order_uid),
    from_state      TEXT,                              -- NULL on first transition
    to_state        TEXT NOT NULL,                     -- intent|submitted|acked|rejected|cancelled|partially_filled|filled|expired
    broker_order_id TEXT,
    reason          TEXT,
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

DDL_FILL_EVENT = """
CREATE TABLE IF NOT EXISTS fill_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    order_uid       TEXT NOT NULL REFERENCES order_master(order_uid),
    broker_fill_id  TEXT NOT NULL UNIQUE,              -- de-dup key
    symbol          TEXT NOT NULL,
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    fees_broker     REAL NOT NULL DEFAULT 0,
    fees_sec        REAL NOT NULL DEFAULT 0,           -- SEC Section 31 (sells only)
    fees_finra_taf  REAL NOT NULL DEFAULT 0,           -- FINRA TAF (sells only, capped)
    is_partial      INTEGER NOT NULL,
    liquidity_flag  TEXT,                              -- M|T|R (maker|taker|removed)
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

DDL_POSITION_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS position_snapshot (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts     TEXT NOT NULL,
    source          TEXT NOT NULL,                     -- bot|broker
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    qty             REAL NOT NULL,
    avg_cost        REAL,
    market_price    REAL,
    market_value    REAL,
    strategy_id     TEXT,                              -- NULL until classified
    classification  TEXT NOT NULL,                     -- bot|external|manual|unknown
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

DDL_STRATEGY_DECISION = """
CREATE TABLE IF NOT EXISTS strategy_decision (
    ledger_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_ts         TEXT NOT NULL,
    strategy_id         TEXT NOT NULL,
    strategy_ver        INTEGER NOT NULL,
    code_hash           TEXT NOT NULL,
    config_hash         TEXT NOT NULL,
    policy_hash         TEXT NOT NULL,                 -- combined hash of all .lock files
    feature_snapshot_id TEXT NOT NULL,                 -- FK to feature store (Phase 2)
    intent_json         TEXT NOT NULL,                 -- canonical intent before risk gate
    risk_decision       TEXT NOT NULL,                 -- accept|reduce|halt
    risk_reason         TEXT,
    emitted_client_order_id TEXT,                      -- nullable if risk halted
    prev_hash           TEXT NOT NULL,
    this_hash           TEXT NOT NULL
);
"""

DDL_RECONCILIATION_PROOF = """
CREATE TABLE IF NOT EXISTS reconciliation_proof (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    recon_ts        TEXT NOT NULL,
    recon_window    TEXT NOT NULL,                     -- intraday|eod|monthly
    bot_hash        TEXT NOT NULL,                     -- sha256 of bot's position vector
    broker_hash     TEXT NOT NULL,                     -- sha256 of broker's position vector
    match           INTEGER NOT NULL,                  -- 0|1
    diff_json       TEXT,                              -- non-null iff match=0
    action_taken    TEXT NOT NULL,                     -- none|halt_new|incident_opened
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

DDL_SCHEMA_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# View — derived current state per order
# ---------------------------------------------------------------------------

DDL_ORDER_CURRENT_VIEW = """
CREATE VIEW IF NOT EXISTS order_current AS
SELECT
    m.order_uid,
    m.client_order_id,
    m.strategy_id,
    m.symbol,
    m.asset_class,
    m.side,
    m.qty,
    m.limit_price,
    m.tif,
    m.origin,
    e.to_state         AS state,
    e.broker_order_id  AS broker_order_id,
    e.event_ts         AS state_ts,
    e.ledger_seq       AS state_ledger_seq
FROM
    order_master m
LEFT JOIN
    order_state_event e ON e.ledger_seq = (
        SELECT MAX(ledger_seq)
        FROM order_state_event
        WHERE order_uid = m.order_uid
    );
"""

# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------

DDL_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_ose_order ON order_state_event(order_uid);",
    "CREATE INDEX IF NOT EXISTS idx_ose_broker ON order_state_event(broker_order_id);",
    "CREATE INDEX IF NOT EXISTS idx_ose_state ON order_state_event(to_state);",
    "CREATE INDEX IF NOT EXISTS idx_fe_order ON fill_event(order_uid);",
    "CREATE INDEX IF NOT EXISTS idx_ps_symbol_ts ON position_snapshot(symbol, snapshot_ts);",
    "CREATE INDEX IF NOT EXISTS idx_sd_strategy_ts ON strategy_decision(strategy_id, decision_ts);",
    "CREATE INDEX IF NOT EXISTS idx_rp_window_ts ON reconciliation_proof(recon_window, recon_ts);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_om_client_order_id ON order_master(client_order_id);",
]

# ---------------------------------------------------------------------------
# Immutability triggers
# ---------------------------------------------------------------------------
#
# SQLite cannot enforce true immutability against a privileged local process
# opening the file directly. These triggers prevent the application path; the
# off-host mirror + daily SHA-256 closure provide the tamper detector.

_APPEND_ONLY_TABLES = (
    "order_master",
    "order_state_event",
    "fill_event",
    "position_snapshot",
    "strategy_decision",
    "reconciliation_proof",
)


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


DDL_TRIGGERS: list[str] = []
for _t in _APPEND_ONLY_TABLES:
    DDL_TRIGGERS.extend(_trigger_ddl(_t))


# ---------------------------------------------------------------------------
# Composite DDL — run in order
# ---------------------------------------------------------------------------

ALL_DDL: list[str] = [
    DDL_SCHEMA_META,
    DDL_ORDER_MASTER,
    DDL_ORDER_STATE_EVENT,
    DDL_FILL_EVENT,
    DDL_POSITION_SNAPSHOT,
    DDL_STRATEGY_DECISION,
    DDL_RECONCILIATION_PROOF,
    DDL_ORDER_CURRENT_VIEW,
    *DDL_INDICES,
    *DDL_TRIGGERS,
]


@dataclass(frozen=True)
class TableSpec:
    """Tiny descriptor used by tests and reconciliation. Lists every event
    table that participates in the hash chain."""

    name: str
    hash_chained: bool


TABLES: tuple[TableSpec, ...] = (
    TableSpec("order_master", hash_chained=False),
    TableSpec("order_state_event", hash_chained=True),
    TableSpec("fill_event", hash_chained=True),
    TableSpec("position_snapshot", hash_chained=True),
    TableSpec("strategy_decision", hash_chained=True),
    TableSpec("reconciliation_proof", hash_chained=True),
)

HASH_CHAINED_TABLES: tuple[str, ...] = tuple(
    t.name for t in TABLES if t.hash_chained
)


def create_ledger(conn) -> None:
    """Apply every DDL statement against the given sqlite3 connection.

    Idempotent: ``IF NOT EXISTS`` everywhere. Caller manages transactions.
    """
    cur = conn.cursor()
    for stmt in ALL_DDL:
        cur.execute(stmt)
    # Stamp schema version + applied marker.
    cur.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES "
        "(?, ?, datetime('now'))",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def read_schema_version(conn) -> int | None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None
