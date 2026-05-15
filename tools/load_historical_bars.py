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

DEFAULT_STOCK_UNIVERSE = (
    "SPY", "QQQ", "IWM", "DIA", "EFA", "EEM",
    "XLK", "XLF", "XLE", "XLV", "TLT",
)
DEFAULT_CRYPTO_UNIVERSE = ("BTC/USD", "ETH/USD")
DEFAULT_UNIVERSE = DEFAULT_STOCK_UNIVERSE   # legacy compatibility


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
    p.add_argument("--stock-symbols", default=",".join(DEFAULT_STOCK_UNIVERSE),
                   help="Comma-separated stock/ETF symbols (yfinance).")
    p.add_argument("--crypto-symbols", default=",".join(DEFAULT_CRYPTO_UNIVERSE),
                   help="Comma-separated crypto symbols (Alpaca format BTC/USD).")
    p.add_argument("--symbols", default=None,
                   help="Legacy: comma-separated list (treated as stocks).")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (overrides --years).")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: today).")
    p.add_argument("--skip-stocks", action="store_true")
    p.add_argument("--skip-crypto", action="store_true")
    p.add_argument("--out", default="data/historical_bars.db")
    args = p.parse_args(argv)

    _load_env()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    if args.start:
        start = dt.date.fromisoformat(args.start)
    else:
        start = end - dt.timedelta(days=365 * args.years)

    # If --symbols is provided, treat as legacy stocks override.
    stock_syms: tuple[str, ...] = ()
    crypto_syms: tuple[str, ...] = ()
    if args.symbols:
        stock_syms = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    else:
        stock_syms = tuple(s.strip().upper() for s in args.stock_symbols.split(",") if s.strip())
        crypto_syms = tuple(s.strip().upper() for s in args.crypto_symbols.split(",") if s.strip())

    if args.skip_stocks:
        stock_syms = ()
    if args.skip_crypto:
        crypto_syms = ()

    print(f"Window: {start} → {end} → {args.out}")
    print(f"Stocks ({len(stock_syms)}): {stock_syms}")
    print(f"Crypto ({len(crypto_syms)}): {crypto_syms}")

    from trading_bot.ingest.data_router import fetch_daily_bars
    from trading_bot.research.historical_bars import open_store, upsert_bars

    all_bars = []
    if stock_syms:
        print(f"Fetching {len(stock_syms)} stocks via yfinance...")
        try:
            bars = fetch_daily_bars(
                symbols=stock_syms, start=start, end=end,
                asset_class="us_equity",
            )
            print(f"  yfinance returned {len(bars)} bar rows.")
            all_bars.extend(bars)
        except Exception as e:
            print(f"  WARN: yfinance failed: {e}", file=sys.stderr)
    if crypto_syms:
        print(f"Fetching {len(crypto_syms)} crypto via Alpaca...")
        try:
            bars = fetch_daily_bars(
                symbols=crypto_syms, start=start, end=end,
                asset_class="crypto",
            )
            print(f"  Alpaca crypto returned {len(bars)} bar rows.")
            all_bars.extend(bars)
        except Exception as e:
            print(f"  WARN: Alpaca crypto failed: {e}", file=sys.stderr)

    if not all_bars:
        print("No bars returned from any source.", file=sys.stderr)
        return 1

    conn = open_store(Path(args.out))
    try:
        n = upsert_bars(conn, all_bars)
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
