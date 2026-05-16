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
    risk_decision       TEXT NOT NULL,                 -- accept|reduce|halt|skip
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

DDL_FEATURE_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS feature_snapshot (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     TEXT NOT NULL UNIQUE,              -- content-addressed (sha256 prefix)
    captured_ts     TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    universe_json   TEXT NOT NULL,                     -- discovery rule output (rule_name, rule_hash, symbols)
    intel_json      TEXT NOT NULL,                     -- {feed_id: {value, source_ts, source_hash}, ...}
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

DDL_DRIFT_EVENT = """
CREATE TABLE IF NOT EXISTS drift_event (
    ledger_seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts             TEXT NOT NULL,
    lane                 TEXT NOT NULL,                -- equity|crypto|<future lanes>
    n_trades             INTEGER NOT NULL,
    modelled_mean_bps    REAL NOT NULL,
    realised_mean_bps    REAL NOT NULL,
    ratio                REAL NOT NULL,                -- realised / modelled
    tolerance_multiplier REAL NOT NULL,                -- breach threshold at write-time
    breach               INTEGER NOT NULL,             -- 0|1
    recommendation       TEXT NOT NULL,                -- ""|"demote:<lane>"
    prev_hash            TEXT NOT NULL,
    this_hash            TEXT NOT NULL
);
"""

# v4 Phase 12 (2026-05-15) — LLM call observability.
DDL_LLM_CALL_EVENT = """
CREATE TABLE IF NOT EXISTS llm_call_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    persona_id      TEXT NOT NULL,
    model           TEXT NOT NULL,
    priority        TEXT NOT NULL,                    -- P0|P1|P2|P3
    input_hash      TEXT NOT NULL,
    cache_hit       INTEGER NOT NULL,                 -- 0|1
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    deferred        INTEGER NOT NULL,                 -- 0|1 was queued then dispatched later
    dropped         INTEGER NOT NULL,                 -- 0|1 P3 over-budget drop
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

# v4 Phase A (2026-05-15) — universe + regime + drift postmortem + paper validation.
DDL_UNIVERSE_AUDIT_EVENT = """
CREATE TABLE IF NOT EXISTS universe_audit_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    universe_size   INTEGER NOT NULL,
    members_json    TEXT NOT NULL,                    -- top-N symbols
    additions_json  TEXT NOT NULL,                    -- joined this window
    removals_json   TEXT NOT NULL,                    -- dropped this window
    turnover_pct    REAL NOT NULL,
    breach          INTEGER NOT NULL,                 -- 0|1
    claude_memo_id  TEXT,                             -- nullable FK
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

DDL_DRIFT_POSTMORTEM_EVENT = """
CREATE TABLE IF NOT EXISTS drift_postmortem_event (
    ledger_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts            TEXT NOT NULL,
    source_event_type   TEXT NOT NULL,                -- drift_event|universe_audit_event|regime_event
    source_ledger_seq   INTEGER NOT NULL,
    persona_id          TEXT NOT NULL,
    persona_hash        TEXT NOT NULL,
    memo_markdown       TEXT NOT NULL,
    prev_hash           TEXT NOT NULL,
    this_hash           TEXT NOT NULL
);
"""

DDL_REGIME_EVENT = """
CREATE TABLE IF NOT EXISTS regime_event (
    ledger_seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts                TEXT NOT NULL,
    asset_class             TEXT NOT NULL,            -- stocks|crypto|options
    prior_regime            TEXT NOT NULL,
    new_regime              TEXT NOT NULL,
    source                  TEXT NOT NULL,            -- classifier|manual|claude_recovery|fast_trigger
    trigger_signals_json    TEXT NOT NULL,
    mandated_actions_json   TEXT NOT NULL,
    claude_memo_id          TEXT,                     -- nullable FK
    prev_hash               TEXT NOT NULL,
    this_hash               TEXT NOT NULL
);
"""

DDL_INTEL_FEATURE_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS intel_feature_snapshot (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    decision_id     TEXT NOT NULL,                    -- joins to strategy_decision
    strategy_id     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    feature_id      TEXT NOT NULL,
    feature_value   TEXT NOT NULL,                    -- canonical JSON of float|str|null
    feed_id         TEXT NOT NULL,
    asof            TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

# v4 Phase C (2026-05-15) — mutation paper-submit validation + review.
DDL_PAPER_VALIDATION_EVENT = """
CREATE TABLE IF NOT EXISTS paper_validation_event (
    ledger_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts            TEXT NOT NULL,
    candidate_id        TEXT NOT NULL,
    strategy_family     TEXT NOT NULL,
    candidate_params    TEXT NOT NULL,                -- canonical JSON
    num_decisions       INTEGER NOT NULL,
    submitted_intents   INTEGER NOT NULL,
    risk_rejected       INTEGER NOT NULL,
    filled_intents      INTEGER NOT NULL,
    avg_slippage_bps    REAL NOT NULL,
    passed              INTEGER NOT NULL,             -- 0|1
    reason              TEXT NOT NULL,
    prev_hash           TEXT NOT NULL,
    this_hash           TEXT NOT NULL
);
"""

DDL_MUTATION_REVIEW_EVENT = """
CREATE TABLE IF NOT EXISTS mutation_review_event (
    ledger_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts            TEXT NOT NULL,
    review_window_iso   TEXT NOT NULL,                -- e.g. "2026-W19"
    persona_id          TEXT NOT NULL,
    persona_hash        TEXT NOT NULL,
    n_candidates        INTEGER NOT NULL,
    n_passed            INTEGER NOT NULL,
    memo_markdown       TEXT NOT NULL,
    prev_hash           TEXT NOT NULL,
    this_hash           TEXT NOT NULL
);
"""

DDL_SEARCH_SPACE_PROPOSAL_EVENT = """
CREATE TABLE IF NOT EXISTS search_space_proposal_event (
    ledger_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts            TEXT NOT NULL,
    review_month_iso    TEXT NOT NULL,                -- e.g. "2026-05"
    persona_id          TEXT NOT NULL,
    persona_hash        TEXT NOT NULL,
    current_hash        TEXT NOT NULL,                -- search_space_v1.json hash at proposal time
    proposed_additions  TEXT NOT NULL,                -- canonical JSON of proposed new dimensions
    memo_markdown       TEXT NOT NULL,
    prev_hash           TEXT NOT NULL,
    this_hash           TEXT NOT NULL
);
"""

# v4 Phase D (2026-05-15) — research bot ledger.
DDL_SOURCE_SCOUT_EVENT = """
CREATE TABLE IF NOT EXISTS source_scout_event (
    ledger_seq                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts                    TEXT NOT NULL,
    source                      TEXT NOT NULL,
    items_seen                  INTEGER NOT NULL,
    items_above_quality         INTEGER NOT NULL,
    items_deduplicated          INTEGER NOT NULL,
    items_candidates_created    INTEGER NOT NULL,
    prev_hash                   TEXT NOT NULL,
    this_hash                   TEXT NOT NULL
);
"""

DDL_STRATEGY_CANDIDATE = """
CREATE TABLE IF NOT EXISTS strategy_candidate (
    ledger_seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts            TEXT NOT NULL,
    source              TEXT NOT NULL,
    source_ref          TEXT NOT NULL,
    raw_content_hash    TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    summary_md          TEXT NOT NULL,
    taxonomy_tags_json  TEXT NOT NULL,
    quality_score       REAL NOT NULL,
    status              TEXT NOT NULL,                -- pending|approved|rejected|implemented
    prev_hash           TEXT NOT NULL,
    this_hash           TEXT NOT NULL
);
"""

DDL_STRATEGY_BLUEPRINT = """
CREATE TABLE IF NOT EXISTS strategy_blueprint (
    ledger_seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts                TEXT NOT NULL,
    candidate_id            INTEGER NOT NULL,
    blueprint_md            TEXT NOT NULL,
    params_json             TEXT NOT NULL,
    universe_filter_json    TEXT NOT NULL,
    data_needs_json         TEXT NOT NULL,
    data_available          INTEGER NOT NULL,         -- 0|1
    intake_transcript_id    TEXT NOT NULL,
    intake_verdict          TEXT NOT NULL,            -- approved|rejected
    prev_hash               TEXT NOT NULL,
    this_hash               TEXT NOT NULL
);
"""

DDL_STRATEGY_CODEGEN_EVENT = """
CREATE TABLE IF NOT EXISTS strategy_codegen_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    blueprint_id    INTEGER NOT NULL,
    new_family_id   TEXT NOT NULL,
    runner_path     TEXT NOT NULL,
    tests_path      TEXT NOT NULL,
    ruff_pass       INTEGER NOT NULL,                 -- 0|1
    mypy_pass       INTEGER NOT NULL,                 -- 0|1
    test_pass       INTEGER NOT NULL,                 -- 0|1
    registered      INTEGER NOT NULL,                 -- 0|1
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

# WS6a — broker swap audit. One row per cutover (alpaca -> webull, etc.).
# The reconciliation kill switch suppresses for 24h after a row here to
# absorb the first night where ledger positions (source='bot' on Alpaca)
# don't match the new broker's positions.
DDL_BROKER_SWITCH_EVENT = """
CREATE TABLE IF NOT EXISTS broker_switch_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    from_broker     TEXT NOT NULL,
    to_broker       TEXT NOT NULL,
    operator        TEXT NOT NULL,
    reason          TEXT,
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

# WS5d — P&L tripwires (realized-loss / drift / execution / behavioural).
# Two-tier severity per tripwire — ``alert`` is observable in cockpit,
# ``halt`` writes a kill_switch_event via the caller.
DDL_ALERT_EVENT = """
CREATE TABLE IF NOT EXISTS alert_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    tripwire        TEXT NOT NULL,       -- realized_loss | drift | exec_quality | behavioural
    severity        TEXT NOT NULL,       -- alert | halt
    observed        REAL NOT NULL,
    threshold       REAL NOT NULL,
    window          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    payload_json    TEXT,
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
);
"""

# WS5f Layer 4 — manual operator halts (PAUSE / FLATTEN) audited
# separately from kill_switch_event so they're attributable to the
# operator's git identity.
DDL_MANUAL_HALT_EVENT = """
CREATE TABLE IF NOT EXISTS manual_halt_event (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TEXT NOT NULL,
    action          TEXT NOT NULL,       -- pause | resume | flatten
    operator        TEXT NOT NULL,
    reason          TEXT,
    source          TEXT NOT NULL,       -- cockpit | cli | hotkey
    payload_json    TEXT,
    prev_hash       TEXT NOT NULL,
    this_hash       TEXT NOT NULL
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
    "CREATE INDEX IF NOT EXISTS idx_fs_strategy_ts ON feature_snapshot(strategy_id, captured_ts);",
    "CREATE INDEX IF NOT EXISTS idx_de_lane_ts ON drift_event(lane, event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_lce_persona_ts ON llm_call_event(persona_id, event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_lce_input_hash ON llm_call_event(input_hash);",
    "CREATE INDEX IF NOT EXISTS idx_uae_strategy_ts ON universe_audit_event(strategy_id, event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_dpe_source ON drift_postmortem_event(source_event_type, source_ledger_seq);",
    "CREATE INDEX IF NOT EXISTS idx_re_class_ts ON regime_event(asset_class, event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_ifs_decision ON intel_feature_snapshot(decision_id);",
    "CREATE INDEX IF NOT EXISTS idx_ifs_strategy_symbol ON intel_feature_snapshot(strategy_id, symbol);",
    "CREATE INDEX IF NOT EXISTS idx_pve_candidate ON paper_validation_event(candidate_id);",
    "CREATE INDEX IF NOT EXISTS idx_mre_window ON mutation_review_event(review_window_iso);",
    "CREATE INDEX IF NOT EXISTS idx_sspe_month ON search_space_proposal_event(review_month_iso);",
    "CREATE INDEX IF NOT EXISTS idx_sse_source_ts ON source_scout_event(source, event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_sc_status ON strategy_candidate(status);",
    "CREATE INDEX IF NOT EXISTS idx_sc_content ON strategy_candidate(raw_content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_sb_candidate ON strategy_blueprint(candidate_id);",
    "CREATE INDEX IF NOT EXISTS idx_sce_family ON strategy_codegen_event(new_family_id);",
    "CREATE INDEX IF NOT EXISTS idx_bse_event_ts ON broker_switch_event(event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_ae_tripwire_ts ON alert_event(tripwire, event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_ae_severity_ts ON alert_event(severity, event_ts);",
    "CREATE INDEX IF NOT EXISTS idx_mhe_action_ts ON manual_halt_event(action, event_ts);",
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
    "feature_snapshot",
    "drift_event",
    "llm_call_event",
    "universe_audit_event",
    "drift_postmortem_event",
    "regime_event",
    "intel_feature_snapshot",
    "paper_validation_event",
    "mutation_review_event",
    "search_space_proposal_event",
    "source_scout_event",
    "strategy_candidate",
    "strategy_blueprint",
    "strategy_codegen_event",
    "broker_switch_event",
    "alert_event",
    "manual_halt_event",
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
    DDL_FEATURE_SNAPSHOT,
    DDL_DRIFT_EVENT,
    DDL_LLM_CALL_EVENT,
    DDL_UNIVERSE_AUDIT_EVENT,
    DDL_DRIFT_POSTMORTEM_EVENT,
    DDL_REGIME_EVENT,
    DDL_INTEL_FEATURE_SNAPSHOT,
    DDL_PAPER_VALIDATION_EVENT,
    DDL_MUTATION_REVIEW_EVENT,
    DDL_SEARCH_SPACE_PROPOSAL_EVENT,
    DDL_SOURCE_SCOUT_EVENT,
    DDL_STRATEGY_CANDIDATE,
    DDL_STRATEGY_BLUEPRINT,
    DDL_STRATEGY_CODEGEN_EVENT,
    DDL_BROKER_SWITCH_EVENT,
    DDL_ALERT_EVENT,
    DDL_MANUAL_HALT_EVENT,
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
    TableSpec("feature_snapshot", hash_chained=True),
    TableSpec("drift_event", hash_chained=True),
    TableSpec("llm_call_event", hash_chained=True),
    TableSpec("universe_audit_event", hash_chained=True),
    TableSpec("drift_postmortem_event", hash_chained=True),
    TableSpec("regime_event", hash_chained=True),
    TableSpec("intel_feature_snapshot", hash_chained=True),
    TableSpec("paper_validation_event", hash_chained=True),
    TableSpec("mutation_review_event", hash_chained=True),
    TableSpec("search_space_proposal_event", hash_chained=True),
    TableSpec("source_scout_event", hash_chained=True),
    TableSpec("strategy_candidate", hash_chained=True),
    TableSpec("strategy_blueprint", hash_chained=True),
    TableSpec("strategy_codegen_event", hash_chained=True),
    TableSpec("broker_switch_event", hash_chained=True),
    TableSpec("alert_event", hash_chained=True),
    TableSpec("manual_halt_event", hash_chained=True),
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


def ensure_schema(conn) -> str:
    """Apply ``ALL_DDL`` against an existing ledger DB when its stored
    ``schema_version`` matches the code's ``SCHEMA_VERSION``. Idempotent
    on every table (``IF NOT EXISTS`` everywhere), so it only adds
    tables/indexes/triggers that were appended within the same version.

    Returns one of:
      * ``"ok"``      — DDL applied (or already current)
      * ``"unstamped"`` — no schema_meta row; DDL applied + version stamped
      * ``"mismatch"``  — stored version != SCHEMA_VERSION; NO-OP, the
        caller (boot_check) reports the mismatch and the operator runs
        a proper migration. Auto-applying across an incompatible bump
        would silently mask the migration.

    Mirrors only carry a subset of tables but accept the full DDL
    because every statement is ``IF NOT EXISTS``.

    This exists because v4 has shipped two *additive* schema growth
    rounds at the same SCHEMA_VERSION=1 (feature_snapshot in Phase 10,
    drift_event in Phase 11). The boot check verifies the chain on
    every hash-chained table, so a table missing from a long-running
    live DB causes a fail-closed daemon refusal. This helper reconciles
    that on startup without re-running the full ``init_ledger.py``
    tool by hand.
    """
    existing = read_schema_version(conn)
    if existing is not None and existing != SCHEMA_VERSION:
        return "mismatch"
    cur = conn.cursor()
    for stmt in ALL_DDL:
        cur.execute(stmt)
    cur.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES "
        "(?, ?, datetime('now'))",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()
    return "unstamped" if existing is None else "ok"
