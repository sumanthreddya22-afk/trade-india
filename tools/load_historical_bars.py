#!/usr/bin/env python
"""Backfill daily bars for the seed-thesis universe.

Pulls split+dividend-adjusted daily bars from Alpaca and writes them to
``data/historical_bars.db``. Idempotent: rerunning replaces existing
rows for the same (symbol, date) keys.

Usage:
  python tools/load_historical_bars.py
  python tools/load_historical_bars.py --years 5
  python tools/load_historical_bars.py --symbols SPY,QQQ,IWM
  python tools/load_historical_bars.py --start 2016-01-01 --end 2025-12-31

Requires ALPACA_API_KEY / ALPACA_API_SECRET in env (via .env).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

DEFAULT_UNIVERSE = ("SPY", "QQQ", "IWM", "DIA", "EFA", "EEM",
                    "XLK", "XLF", "XLE", "XLV")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--years", type=int, default=10,
                   help="Years of history to pull (default: 10).")
    p.add_argument("--symbols", default=",".join(DEFAULT_UNIVERSE),
                   help="Comma-separated symbol list.")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (overrides --years).")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: today).")
    p.add_argument("--out", default="data/historical_bars.db")
    args = p.parse_args(argv)

    _load_env()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    if args.start:
        start = dt.date.fromisoformat(args.start)
    else:
        start = end - dt.timedelta(days=365 * args.years)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    print(f"Loading {len(symbols)} symbols, {start} → {end} → {args.out}")
    print(f"Symbols: {symbols}")

    from trading_bot.research.historical_bars import (
        fetch_bars_from_alpaca, open_store, upsert_bars,
    )

    try:
        bars = fetch_bars_from_alpaca(
            symbols=symbols, start=start, end=end,
        )
    except Exception as e:
        print(f"FAIL: Alpaca fetch: {e}", file=sys.stderr)
        return 1
    print(f"Alpaca returned {len(bars)} bar rows.")
    if not bars:
        print("No bars returned; check creds + plan.", file=sys.stderr)
        return 1

    conn = open_store(Path(args.out))
    try:
        n = upsert_bars(conn, bars)
    finally:
        conn.close()
    print(f"Wrote {n} rows to {args.out}.")

    # Quick verification
    conn = open_store(Path(args.out))
    try:
        cur = conn.execute(
            "SELECT symbol, COUNT(*) AS n, MIN(bar_date) AS first, MAX(bar_date) AS last "
            "FROM bar_daily GROUP BY symbol ORDER BY symbol"
        )
        print("\nPer-symbol coverage:")
        print(f"  {'symbol':<8} {'count':>6} {'first':>12} {'last':>12}")
        for row in cur.fetchall():
            print(f"  {row[0]:<8} {row[1]:>6} {row[2]:>12} {row[3]:>12}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
