#!/usr/bin/env python3
"""Verify the v4 ledger hash chain.

Walks every hash-chained table in ``ledger.db`` (and optionally the
mirror), recomputing each row's ``this_hash`` and comparing to the
stored value. Exits non-zero on the first mismatch.

Intended for nightly cron + kernel boot. Phase 2 will wire the boot
path; Phase 1 supplies the standalone CLI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_bot.ledger import (
    DEFAULT_LEDGER_PATH,
    DEFAULT_MIRROR_PATH,
    HashChainBroken,
    connect_reader,
    verify_all_chained,
)


def _verify_one(path: Path) -> int:
    if not path.exists():
        print(f"SKIP: {path} does not exist (run tools/init_ledger.py first)")
        return 0
    conn = connect_reader(path)
    try:
        result = verify_all_chained(conn)
    except HashChainBroken as e:
        print(f"FAIL {path}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    n = sum(result.values())
    print(f"OK {path}: {n} rows verified across {len(result)} tables")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db", type=Path, default=REPO_ROOT / DEFAULT_LEDGER_PATH,
    )
    ap.add_argument(
        "--mirror", type=Path, default=REPO_ROOT / DEFAULT_MIRROR_PATH,
    )
    ap.add_argument("--skip-mirror", action="store_true")
    args = ap.parse_args()

    rc = _verify_one(args.db)
    if not args.skip_mirror:
        rc = max(rc, _verify_one(args.mirror))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
