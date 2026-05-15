#!/usr/bin/env python3
"""Register v3 strategy families at research_only.

Runs once per Phase-A bring-up. Idempotent: re-running on an existing
strategy_version row is a no-op (the UNIQUE constraint guards it).

The fast-track lock + paper_validation later promote these to
``tiny_paper`` via ``registry.auto_register``; this script only seeds
the initial registration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_bot.ledger import (  # noqa: E402
    DEFAULT_LEDGER_PATH, connect_writer, ensure_schema,
)
from trading_bot.registry.strategies import register_version  # noqa: E402


V3_FAMILIES = [
    {
        "strategy_id": "ETF_MOMENTUM_v3",
        "strategy_ver": 1,
        "thesis_id": "etf_momentum_v3_seed",
        "hypothesis_id": "h-etf-v3",
        "lane": "etf_momentum",
        "owner": "operator",
    },
    {
        "strategy_id": "DUAL_MOMENTUM_v3",
        "strategy_ver": 1,
        "thesis_id": "dual_momentum_v3_seed",
        "hypothesis_id": "h-dual-v3",
        "lane": "dual_momentum",
        "owner": "operator",
    },
    {
        "strategy_id": "CRYPTO_MOMENTUM_v3",
        "strategy_ver": 1,
        "thesis_id": "crypto_momentum_v3_seed",
        "hypothesis_id": "h-crypto-v3",
        "lane": "crypto_trend",
        "owner": "operator",
    },
    {
        "strategy_id": "SPY_WHEEL_v3",
        "strategy_ver": 1,
        "thesis_id": "spy_wheel_v3_seed",
        "hypothesis_id": "h-wheel-v3",
        "lane": "options_income_wheel",
        "owner": "operator",
    },
]


def _module_hash(strategy_id: str) -> str:
    """Stable code hash based on the module path. Real hash should
    walk the module's bytes once the registry stabilises."""
    return hashlib.sha256(strategy_id.encode()).hexdigest()[:32]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=REPO_ROOT / DEFAULT_LEDGER_PATH)
    args = ap.parse_args()

    if not args.ledger.exists():
        print(f"ERROR: ledger missing at {args.ledger}", file=sys.stderr)
        return 1

    conn = connect_writer(args.ledger)
    try:
        ensure_schema(conn)
        for entry in V3_FAMILIES:
            sid = entry["strategy_id"]
            ver = entry["strategy_ver"]
            cur = conn.execute(
                "SELECT 1 FROM strategy_version WHERE strategy_id=? AND strategy_ver=?",
                (sid, ver),
            )
            if cur.fetchone():
                print(f"  • {sid} v{ver} already registered")
                continue
            register_version(
                conn,
                strategy_id=sid, strategy_ver=ver,
                code_hash=_module_hash(sid),
                config_hash=_module_hash(f"{sid}-cfg"),
                thesis_id=entry["thesis_id"],
                hypothesis_id=entry["hypothesis_id"],
                lane=entry["lane"],
                owner=entry["owner"],
            )
            print(f"  ✓ registered {sid} v{ver} at research_only")
        conn.commit()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
