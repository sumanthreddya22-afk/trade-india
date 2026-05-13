"""Kernel boot — startup integrity checks.

Plan v4 §6: at startup the kernel reads policy/HASHES, recomputes
SHA-256 of each .lock file, and refuses to start on any mismatch.
Plan v4 §14 P0: hash chain verified at startup.

``run_boot_checks`` is the single entry point. It runs:

  1. SQLite integrity_check on the ledger + mirror
  2. Schema version match
  3. Policy hash verification
  4. Hash-chain verification (ledger + mirror)
  5. Active kill switches surfaced (not a failure on its own)

Returns a ``BootReport`` dataclass. ``ok`` is True only if every gate
passes. The CLI driver (``tools/boot_check.py``) exits non-zero when
``ok`` is False.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from trading_bot.ledger import (
    DEFAULT_LEDGER_PATH, DEFAULT_MIRROR_PATH, SCHEMA_VERSION,
    HashChainBroken, connect_reader, read_schema_version,
    verify_all_chained,
)
from trading_bot.risk import (
    DEFAULT_POLICY_DIR, PolicyHashMismatch, active_kills,
    ensure_kill_switch_table, verify_policy_hashes,
)


@dataclass
class BootReport:
    ok: bool = True
    checks: List[dict] = field(default_factory=list)
    active_kills: List[str] = field(default_factory=list)

    def _record(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"name": name, "status": status, "detail": detail})
        if status not in ("ok", "info"):
            self.ok = False


def _integrity_check(db: Path) -> str:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        row = cur.fetchone()
        return row[0] if row else "unknown"
    finally:
        conn.close()


def run_boot_checks(
    ledger_db: Path = None,
    mirror_db: Path = None,
    policy_dir: Path = DEFAULT_POLICY_DIR,
) -> BootReport:
    ledger_db = ledger_db or Path.cwd() / DEFAULT_LEDGER_PATH
    mirror_db = mirror_db or Path.cwd() / DEFAULT_MIRROR_PATH
    rep = BootReport()

    # 1. integrity_check on ledger + mirror
    for label, db in (("ledger", ledger_db), ("mirror", mirror_db)):
        if not db.exists():
            rep._record(f"integrity_check:{label}", "missing",
                        f"{db} does not exist")
            continue
        result = _integrity_check(db)
        if result.strip().lower() == "ok":
            rep._record(f"integrity_check:{label}", "ok")
        else:
            rep._record(f"integrity_check:{label}", "fail", result)

    # 2. schema_version match
    for label, db in (("ledger", ledger_db), ("mirror", mirror_db)):
        if not db.exists():
            continue
        conn = connect_reader(db)
        try:
            sv = read_schema_version(conn)
            if sv == SCHEMA_VERSION:
                rep._record(f"schema_version:{label}", "ok", f"v={sv}")
            else:
                rep._record(
                    f"schema_version:{label}", "fail",
                    f"db={sv} expected={SCHEMA_VERSION}",
                )
        finally:
            conn.close()

    # 3. policy hash verification
    try:
        verify_policy_hashes(policy_dir=policy_dir,
                             hashes_path=policy_dir / "HASHES")
        rep._record("policy_hashes", "ok")
    except PolicyHashMismatch as e:
        rep._record("policy_hashes", "fail", str(e))
    except FileNotFoundError as e:
        rep._record("policy_hashes", "fail", str(e))

    # 4. hash-chain verification
    for label, db in (("ledger", ledger_db), ("mirror", mirror_db)):
        if not db.exists():
            continue
        conn = connect_reader(db)
        try:
            verify_all_chained(conn)
            rep._record(f"hash_chain:{label}", "ok")
        except HashChainBroken as e:
            rep._record(f"hash_chain:{label}", "fail", str(e))
        finally:
            conn.close()

    # 5. surface active kill switches (informational; not a boot failure)
    if ledger_db.exists():
        # We need a writer to ensure_kill_switch_table; but read-only
        # suffices for active_kills. If the table doesn't exist yet,
        # active_kills() raises — fall back to empty.
        try:
            conn = connect_reader(ledger_db)
            try:
                kills = sorted(active_kills(conn))
            finally:
                conn.close()
        except sqlite3.OperationalError:
            kills = []
        rep.active_kills = kills
        rep._record("active_kills", "info",
                    ", ".join(kills) if kills else "(none)")

    return rep


__all__ = ["BootReport", "run_boot_checks"]
