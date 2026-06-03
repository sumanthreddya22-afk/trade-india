"""ensure_schema: daemon-startup auto-reconciliation of additive DDL.

Regression: Phase 10 + Phase 11 both shipped new tables under the same
SCHEMA_VERSION=1 (feature_snapshot, drift_event). Live ledgers that
existed before those phases were missing the tables, and the boot
check's hash-chain verification failed with ``no such table``. The
fix runs the full DDL (idempotent IF-NOT-EXISTS) on startup when
versions match; refuses across version mismatches so true migrations
aren't silently masked.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stub_claude_cli_for_boot_check(monkeypatch):
    """Boot check includes a `claude_cli` probe — stub the path so the
    test doesn't require the Claude CLI installed locally."""
    monkeypatch.setenv("TRADING_BOT_CLAUDE_CLI_PATH", "/bin/echo")

from trading_bot.ledger import (
    SCHEMA_VERSION, connect_writer, ensure_schema,
)
from trading_bot.ledger.schema import (
    DDL_DRIFT_EVENT, DDL_FEATURE_SNAPSHOT, DDL_ORDER_MASTER,
    DDL_SCHEMA_META,
)


def _legacy_ledger(path: Path, *, stamp_version: int | None = 1) -> None:
    """Build a v1-shaped DB missing the Phase-10/11 additive tables."""
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    # Bring the schema_meta + a couple of tables to life — but stop
    # short of the newer additive tables. This simulates a long-running
    # paper daemon whose DB was initialised before Phase 10.
    cur.execute(DDL_SCHEMA_META)
    cur.execute(DDL_ORDER_MASTER)
    if stamp_version is not None:
        cur.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) "
            "VALUES ('schema_version', ?, datetime('now'))",
            (str(stamp_version),),
        )
    conn.commit()
    conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    finally:
        conn.close()


def test_adds_missing_tables_when_version_matches(tmp_path) -> None:
    p = tmp_path / "ledger.db"
    _legacy_ledger(p, stamp_version=SCHEMA_VERSION)
    before = _table_names(p)
    assert "feature_snapshot" not in before
    assert "drift_event" not in before

    conn = connect_writer(p)
    try:
        status = ensure_schema(conn)
    finally:
        conn.close()
    assert status == "ok"

    after = _table_names(p)
    assert {"feature_snapshot", "drift_event"} <= after


def test_no_op_when_already_complete(tmp_path) -> None:
    """Running twice in a row is safe — the second call applies
    nothing new but still reports 'ok'."""
    p = tmp_path / "ledger.db"
    _legacy_ledger(p, stamp_version=SCHEMA_VERSION)
    conn = connect_writer(p)
    try:
        ensure_schema(conn)
        ensure_schema(conn)
        tables_post = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    finally:
        conn.close()
    assert "feature_snapshot" in tables_post
    assert "drift_event" in tables_post


def test_refuses_to_apply_across_version_mismatch(tmp_path) -> None:
    """Critical safety: an incompatible SCHEMA_VERSION bump in code
    must NOT be silently overwritten by ensure_schema. Boot check then
    surfaces the mismatch and the operator runs the real migration."""
    p = tmp_path / "ledger.db"
    # Stamp a *future* version on disk so SCHEMA_VERSION (current code)
    # is older. Or vice versa — the helper rejects either way.
    _legacy_ledger(p, stamp_version=SCHEMA_VERSION + 99)

    conn = connect_writer(p)
    try:
        status = ensure_schema(conn)
        # schema_meta value must NOT have been clobbered.
        stamped = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "mismatch"
    assert int(stamped) == SCHEMA_VERSION + 99
    # And no new tables were materialised.
    assert "feature_snapshot" not in _table_names(p)


def test_unstamped_db_gets_stamped(tmp_path) -> None:
    """A legacy DB that never wrote schema_meta gets DDL + a stamp.
    Reported status differentiates from the steady-state 'ok' path so
    log readers can spot first-time reconciliation."""
    p = tmp_path / "ledger.db"
    _legacy_ledger(p, stamp_version=None)

    conn = connect_writer(p)
    try:
        status = ensure_schema(conn)
        stamped = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert status == "unstamped"
    assert stamped is not None
    assert int(stamped[0]) == SCHEMA_VERSION
    assert {"feature_snapshot", "drift_event"} <= _table_names(p)


def test_daemon_startup_runs_ensure_schema(tmp_path, caplog) -> None:
    """End-to-end: a daemon started against a partial DB should
    reconcile and pass its own boot check on the same run."""
    from trading_bot.daemon import DaemonConfig, run_daemon
    from trading_bot.daemon.jobs import DaemonContext

    ledger = tmp_path / "ledger.db"
    mirror = tmp_path / "mirror.db"
    _legacy_ledger(ledger, stamp_version=SCHEMA_VERSION)
    _legacy_ledger(mirror, stamp_version=SCHEMA_VERSION)

    ctx = DaemonContext(
        ledger_db=ledger, mirror_db=mirror,
        policy_dir=Path(__file__).resolve().parent.parent / "policy",
    )
    config = DaemonConfig(
        run_boot_check_on_startup=True, enable_file_logging=False,
    )
    rc = run_daemon(ctx=ctx, config=config, once=True)
    assert rc == 0
    # Both DBs now carry the additive tables.
    assert {"feature_snapshot", "drift_event"} <= _table_names(ledger)
    assert {"feature_snapshot", "drift_event"} <= _table_names(mirror)
