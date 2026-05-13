#!/usr/bin/env python3
"""One-shot initialiser for the v4 ledger.

Creates ``data/ledger/ledger.db`` and the off-host mirror at
``data/ledger/mirror.db``. Idempotent — running twice is safe.

Refuses to run against a DB whose stored ``schema_version`` differs from
``ledger.SCHEMA_VERSION`` — that's a migration scenario, not init.
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
    SCHEMA_VERSION,
    acquire_writer_lock,
    connect_writer,
    create_ledger,
    init_mirror,
    read_schema_version,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db", type=Path, default=REPO_ROOT / DEFAULT_LEDGER_PATH,
        help="ledger DB path (default data/ledger/ledger.db)",
    )
    ap.add_argument(
        "--mirror", type=Path, default=REPO_ROOT / DEFAULT_MIRROR_PATH,
        help="mirror DB path (default data/ledger/mirror.db)",
    )
    args = ap.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.mirror.parent.mkdir(parents=True, exist_ok=True)

    with acquire_writer_lock(args.db):
        conn = connect_writer(args.db)
        existing = read_schema_version(conn)
        if existing is not None and existing != SCHEMA_VERSION:
            print(
                f"REFUSE: {args.db} is schema_version={existing}; this "
                f"script writes version={SCHEMA_VERSION}. Migration needed.",
                file=sys.stderr,
            )
            return 1
        create_ledger(conn)
        conn.close()

    mirror = init_mirror(args.mirror)
    mirror.close()

    print(f"ledger ready: {args.db}")
    print(f"mirror ready: {args.mirror}")
    print(f"schema_version: {SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
