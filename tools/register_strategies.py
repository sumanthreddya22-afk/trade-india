#!/usr/bin/env python
"""Register every v4 strategy at research_only.

Idempotent: re-running just confirms the existing rows. Each strategy
gets exactly one initial version (v1) registered against the seed
thesis. Promotion to shadow/tiny_paper/live happens via
``bot strategy promote`` after Tier-1 (or the wheel backtest-lite)
produces a passing validation_artifact.

Use:
  python tools/register_strategies.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def main() -> int:
    _load_env()
    from trading_bot.ledger import DEFAULT_LEDGER_PATH, connect_writer
    from trading_bot.registry import (
        VersionNotFound, get_active_version, register_version,
    )
    from trading_bot.registry.schema import ensure_registry_tables

    ledger = Path.cwd() / DEFAULT_LEDGER_PATH
    if not ledger.exists():
        print(f"FAIL: {ledger} does not exist. Run tools/init_ledger.py first.",
              file=sys.stderr)
        return 1

    registrations = [
        # (strategy_id, lane, thesis_id, hypothesis_id, code_hash_seed)
        ("ETF_MOMENTUM_v1", "etf_momentum",
         "edge_thesis_v1", "etf_momentum_12_1.h1",
         "trading_bot.strategies.etf_momentum_v1"),
        ("DUAL_MOMENTUM_v1", "dual_momentum",
         "edge_thesis_v1.dual", "dual_momentum_spy_tlt.h1",
         "trading_bot.strategies.dual_momentum_v1"),
        ("CRYPTO_MOMENTUM_v1", "crypto_trend",
         "edge_thesis_crypto_v1", "crypto_momentum_btc_eth.h1",
         "trading_bot.strategies.crypto_momentum_v1"),
        ("SPY_WHEEL_v1", "options_income_wheel",
         "edge_thesis_wheel_v1", "spy_wheel_csp.h1",
         "trading_bot.strategies.spy_wheel_v1"),
    ]

    conn = connect_writer(ledger)
    ensure_registry_tables(conn)

    summary = []
    for sid, lane, thesis_id, hyp, code_seed in registrations:
        # Skip if already present.
        try:
            current = get_active_version(conn, sid)
            summary.append({
                "strategy_id": sid, "status": "already_registered",
                "current_version": current.strategy_ver,
                "current_status": current.status,
            })
            continue
        except VersionNotFound:
            pass

        code_hash = hashlib.sha256(code_seed.encode()).hexdigest()
        config_hash = hashlib.sha256(f"{sid}:default".encode()).hexdigest()
        ver = register_version(
            conn,
            strategy_id=sid, strategy_ver=1,
            code_hash=code_hash, config_hash=config_hash,
            thesis_id=thesis_id, hypothesis_id=hyp,
            validation_artifact_id=None, lane=lane,
            status="research_only", expiry_date=None,
            owner="operator (Phase 9 bootstrap)",
        )
        conn.commit()
        summary.append({
            "strategy_id": sid, "status": "registered_new",
            "version": ver.strategy_ver,
            "lane": ver.lane, "registry_status": ver.status,
        })

    conn.close()

    print(f"Registered {len(summary)} strategies:")
    for row in summary:
        print(f"  {row}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
