#!/usr/bin/env python3
"""One-shot: register the ETF Momentum v1 seed strategy.

Plan v4 §2: "the only thesis the system harvests until paper proof
passes. The mutation engine (Section 8) explores variants of this
thesis — not 50 unrelated ideas."

The seed lands at status='research_only'. Phase 5 ships the research
factory that produces the first Tier-1 (research_candidate) artifact;
that artifact will let the operator promote to 'shadow'.

Idempotent: if the strategy already exists at this version, the script
prints the existing row and exits 0.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_bot.ledger import (
    DEFAULT_LEDGER_PATH, acquire_writer_lock, connect_writer,
)
from trading_bot.registry import (
    VersionNotFound, get_active_version, register_version,
)

SEED_STRATEGY_ID = "ETF_MOMENTUM_v1"
SEED_STRATEGY_VER = 1
SEED_THESIS_ID = "edge_thesis_v1"
SEED_HYPOTHESIS_ID = "edge_thesis_v1"   # same as thesis for the seed
SEED_LANE = "etf_momentum"
SEED_OWNER = "solo-operator"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path,
                    default=REPO_ROOT / DEFAULT_LEDGER_PATH)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"REFUSE: {args.db} does not exist; run tools/init_ledger.py first",
              file=sys.stderr)
        return 1

    code_hash = _hash_text(
        "ETF_MOMENTUM_v1 strategy placeholder — implementation lands "
        "alongside research factory in Phase 5; the registry row is the "
        "anchor that lets validation artifacts attach to it."
    )
    config_hash = _hash_text(
        '{"thesis_id":"edge_thesis_v1","status":"research_only"}'
    )

    with acquire_writer_lock(args.db):
        conn = connect_writer(args.db)
        try:
            try:
                existing = get_active_version(conn, SEED_STRATEGY_ID)
                print(f"already registered: {SEED_STRATEGY_ID} v{existing.strategy_ver}"
                      f" status={existing.status} thesis={existing.thesis_id}")
                return 0
            except VersionNotFound:
                pass

            version = register_version(
                conn,
                strategy_id=SEED_STRATEGY_ID, strategy_ver=SEED_STRATEGY_VER,
                code_hash=code_hash, config_hash=config_hash,
                thesis_id=SEED_THESIS_ID, hypothesis_id=SEED_HYPOTHESIS_ID,
                lane=SEED_LANE, owner=SEED_OWNER,
                status="research_only",
                validation_artifact_id=None, expiry_date=None,
            )
            print(
                f"registered: {version.strategy_id} v{version.strategy_ver} "
                f"thesis={version.thesis_id} lane={version.lane} "
                f"status={version.status}"
            )
            return 0
        finally:
            conn.close()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
