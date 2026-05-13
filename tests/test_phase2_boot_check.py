"""Phase 2 — kernel boot check end-to-end."""
from __future__ import annotations

import shutil
from pathlib import Path

from trading_bot.kernel import run_boot_checks
from trading_bot.ledger import create_ledger, init_mirror
from trading_bot.ledger.connection import connect_writer
from trading_bot.risk.policy_loader import DEFAULT_POLICY_DIR


def _init_dbs(tmp_path: Path):
    ledger = tmp_path / "ledger.db"
    mirror = tmp_path / "mirror.db"
    c = connect_writer(ledger)
    create_ledger(c)
    c.close()
    m = init_mirror(mirror)
    m.close()
    return ledger, mirror


def test_boot_ok_with_fresh_dbs(tmp_path: Path) -> None:
    ledger, mirror = _init_dbs(tmp_path)
    rep = run_boot_checks(ledger_db=ledger, mirror_db=mirror,
                          policy_dir=DEFAULT_POLICY_DIR)
    assert rep.ok
    statuses = {c["name"]: c["status"] for c in rep.checks}
    assert statuses["policy_hashes"] == "ok"
    assert statuses["hash_chain:ledger"] == "ok"
    assert statuses["integrity_check:ledger"] == "ok"


def test_boot_fails_on_corrupt_policy(tmp_path: Path) -> None:
    ledger, mirror = _init_dbs(tmp_path)
    policy = tmp_path / "policy"
    shutil.copytree(DEFAULT_POLICY_DIR, policy)
    target = policy / "risk_policy.lock"
    target.write_text(target.read_text() + "\n")
    rep = run_boot_checks(ledger_db=ledger, mirror_db=mirror,
                          policy_dir=policy)
    assert not rep.ok
    assert any(
        c["name"] == "policy_hashes" and c["status"] == "fail"
        for c in rep.checks
    )


def test_boot_missing_ledger_is_recorded(tmp_path: Path) -> None:
    rep = run_boot_checks(ledger_db=tmp_path / "absent.db",
                          mirror_db=tmp_path / "absent_mirror.db",
                          policy_dir=DEFAULT_POLICY_DIR)
    assert not rep.ok
    statuses = {c["name"]: c["status"] for c in rep.checks}
    assert statuses.get("integrity_check:ledger") == "missing"
