#!/usr/bin/env python3
"""Kernel boot check — run before any new daemon start.

Exits 0 iff every Phase 2 boot gate passes (schema version, integrity,
policy hashes, hash chain). Active kill switches are surfaced but do
not cause a non-zero exit; the daemon honours them at runtime.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_bot.kernel import run_boot_checks
from trading_bot.ledger import DEFAULT_LEDGER_PATH, DEFAULT_MIRROR_PATH
from trading_bot.risk import DEFAULT_POLICY_DIR


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=REPO_ROOT / DEFAULT_LEDGER_PATH)
    ap.add_argument("--mirror", type=Path, default=REPO_ROOT / DEFAULT_MIRROR_PATH)
    ap.add_argument("--policy", type=Path, default=DEFAULT_POLICY_DIR)
    args = ap.parse_args()

    rep = run_boot_checks(
        ledger_db=args.ledger, mirror_db=args.mirror, policy_dir=args.policy,
    )
    for c in rep.checks:
        mark = {"ok": "✓", "info": "•", "fail": "✗", "missing": "?"}.get(
            c["status"], "?",
        )
        print(f"  {mark} {c['name']:<26s} {c['status']:<8s} {c['detail']}")
    print()
    print("OK" if rep.ok else "FAIL")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
