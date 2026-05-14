#!/usr/bin/env python
"""Read-only Alpaca smoke test.

Run this from the main checkout (which has .env) BEFORE starting the
daemon, to confirm:

  1. Alpaca creds load from .env.
  2. The trading client can authenticate.
  3. The data client can fetch a bar for SPY.

Nothing is written to the ledger. Nothing is submitted to Alpaca. This
is the absolute minimum proof that the adapter works against your live
paper account.

Usage:
  cd ~/Trading
  source .venv/bin/activate
  python tools/alpaca_smoke.py

Exit codes:
  0 — everything works
  1 — config / network / auth failure (read stderr for detail)
"""
from __future__ import annotations

import os
import sys


def _load_env() -> None:
    """Load .env via python-dotenv if present, otherwise rely on shell."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _redact(s: str) -> str:
    if not s:
        return "(empty)"
    return f"{s[:4]}…{s[-2:]} (len={len(s)})"


def main() -> int:
    _load_env()
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET", "").strip()
    if not key or not secret:
        print("FAIL: ALPACA_API_KEY / ALPACA_API_SECRET missing.", file=sys.stderr)
        print("Add them to .env in the repo root and retry.", file=sys.stderr)
        return 1
    print(f"creds: ALPACA_API_KEY={_redact(key)}")

    from trading_bot.ingest.alpaca_adapter import AlpacaAdapter

    try:
        adapter = AlpacaAdapter()
    except Exception as e:
        print(f"FAIL: adapter construction: {e}", file=sys.stderr)
        return 1

    # 1) Account
    acct = adapter.fetch_account()
    if not acct:
        print("FAIL: fetch_account returned empty", file=sys.stderr)
        return 1
    print(f"account: equity=${acct.get('equity'):,.2f}  "
          f"cash=${acct.get('cash'):,.2f}  "
          f"buying_power=${acct.get('buying_power'):,.2f}  "
          f"status={acct.get('status')}")

    # 2) Positions
    positions = adapter.fetch_positions()
    print(f"positions: {len(positions)} rows")
    for p in positions[:5]:
        print(f"  {p['symbol']:<10} qty={p['qty']:>10.4f}  "
              f"value=${p['market_value']:>12,.2f}  class={p['asset_class']}")
    if len(positions) > 5:
        print(f"  … and {len(positions) - 5} more")

    # 3) Bars (just SPY, latest 1 bar)
    bars = adapter.fetch_latest_bars(symbols=("SPY",))
    if not bars or "SPY" not in bars:
        print("WARN: fetch_latest_bars returned no SPY bar "
              "(market may be closed; data plan may not include this).",
              file=sys.stderr)
    else:
        b = bars["SPY"]
        print(f"bars: SPY close={b['close']:.2f}  ts={b['ts']}")

    print("\nOK — Alpaca adapter authenticates and reads data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
