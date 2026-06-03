#!/usr/bin/env python
"""Backfill daily bars for the Indian-market paper trading universe.

Pulls split+dividend-adjusted daily bars from yfinance (free, no API
key) and writes them to ``data/historical_bars.db``. Idempotent:
rerunning replaces existing rows for the same (symbol, date) keys.

yfinance symbol mapping is handled automatically:
  * NSE equities/ETFs: RELIANCE -> RELIANCE.NS
  * Crypto INR pairs:  BTC/INR  -> BTC-INR
  * NSE indices:       NIFTY    -> ^NSEI

Usage:
  # Full universe (ETFs + large-caps + crypto), 5 years
  python tools/load_historical_bars.py

  # Just ETFs, 10 years
  python tools/load_historical_bars.py --years 10 --skip-stocks --skip-crypto

  # Custom symbols
  python tools/load_historical_bars.py --stock-symbols RELIANCE,TCS,INFY

  # Specific date range
  python tools/load_historical_bars.py --start 2020-01-01 --end 2025-12-31
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

# ── NSE Paper Trading Universe ───────────────────────────────────────────────
# Selected for: liquidity, sector diversity, strategy compatibility.

# ETFs — used by etf_momentum_v1/v3 and dual_momentum_v1/v3
NSE_ETF_UNIVERSE = (
    "NIFTYBEES",     # Nippon Nifty 50 ETF — most liquid ETF in India
    "JUNIORBEES",    # Nippon Nifty Next 50 ETF — mid-cap tilt
    "BANKBEES",      # Nippon Bank Nifty ETF — banking sector
    "GOLDBEES",      # Nippon Gold ETF — uncorrelated to equity
    "LIQUIDBEES",    # Nippon Liquid ETF — cash parking / risk-off
    "SETFNIF50",     # SBI Nifty 50 ETF — alternative tracker
    "MON100",        # Motilal Oswal Nifty Midcap 100 Momentum ETF
    "ITBEES",        # Nippon India IT ETF — sector ETF
)

# Large-cap stocks — high liquidity, sector diversity
NSE_STOCK_UNIVERSE = (
    "RELIANCE",      # Reliance Industries — conglomerate, most traded
    "TCS",           # Tata Consultancy — IT, stable
    "HDFCBANK",      # HDFC Bank — largest private bank
    "INFY",          # Infosys — IT, good momentum characteristics
    "ICICIBANK",     # ICICI Bank — 2nd largest private bank
    "ITC",           # ITC — FMCG/Hotels, range-bound (tests "no signal")
    "SBIN",          # State Bank of India — highest volume PSU
    "BHARTIARTL",    # Bharti Airtel — telecom, strong trend
    "LT",            # Larsen & Toubro — infra, cyclical
    "M&M",           # Mahindra & Mahindra — auto, Nifty weight
    "HINDUNILVR",    # Hindustan Unilever — FMCG defensive
    "KOTAKBANK",     # Kotak Mahindra Bank — private bank
    "MARUTI",        # Maruti Suzuki — auto, Nifty weight
    "SUNPHARMA",     # Sun Pharma — pharma sector leader
    "TITAN",         # Titan Company — consumer, strong trend
    "AXISBANK",      # Axis Bank — private bank
    "WIPRO",         # Wipro — IT
    "BAJFINANCE",    # Bajaj Finance — NBFC, high beta
    "HCLTECH",       # HCL Technologies — IT
    "ADANIENT",      # Adani Enterprises — diversified
)

# Indices — used for regime detection + wheel strategy
NSE_INDEX_UNIVERSE = (
    "NIFTY",         # Nifty 50 — main benchmark
    "BANKNIFTY",     # Bank Nifty — banking benchmark
)

# Crypto INR pairs — used by crypto_momentum_v1/v3 (capped at 15% equity)
CRYPTO_INR_UNIVERSE = (
    "BTC/INR",       # Bitcoin INR — highest crypto liquidity in India
    "ETH/INR",       # Ethereum INR — 2nd highest
)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _fetch_batch(
    symbols: tuple[str, ...],
    start: dt.date,
    end: dt.date,
    asset_class: str,
    label: str,
) -> list:
    """Fetch bars for a batch with retry + progress."""
    from trading_bot.ingest.data_router import fetch_daily_bars

    print(f"\nFetching {len(symbols)} {label}...")
    print(f"  Symbols: {', '.join(symbols)}")

    # yfinance can be flaky — retry once on empty result
    for attempt in range(2):
        try:
            bars = fetch_daily_bars(
                symbols=symbols, start=start, end=end,
                asset_class=asset_class,
            )
            if bars:
                # Count per-symbol
                by_sym: dict[str, int] = {}
                for b in bars:
                    by_sym[b.symbol] = by_sym.get(b.symbol, 0) + 1
                print(f"  Got {len(bars)} bar rows across {len(by_sym)} symbols")
                for sym, n in sorted(by_sym.items()):
                    print(f"    {sym:<14} {n:>5} days")
                # Report symbols with no data
                missing = set(symbols) - set(by_sym.keys())
                if missing:
                    print(f"  WARNING: no data for: {', '.join(sorted(missing))}")
                return bars
            if attempt == 0:
                print("  Empty result, retrying in 3s...")
                time.sleep(3)
        except Exception as e:
            print(f"  WARN: {label} fetch failed: {e}", file=sys.stderr)
            if attempt == 0:
                time.sleep(3)

    print(f"  WARN: {label} returned 0 rows after retries", file=sys.stderr)
    return []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--years", type=int, default=5,
                   help="Years of history to pull (default: 5).")
    p.add_argument("--etf-symbols", default=",".join(NSE_ETF_UNIVERSE),
                   help="Comma-separated NSE ETF symbols.")
    p.add_argument("--stock-symbols", default=",".join(NSE_STOCK_UNIVERSE),
                   help="Comma-separated NSE stock symbols.")
    p.add_argument("--index-symbols", default=",".join(NSE_INDEX_UNIVERSE),
                   help="Comma-separated NSE index symbols.")
    p.add_argument("--crypto-symbols", default=",".join(CRYPTO_INR_UNIVERSE),
                   help="Comma-separated crypto INR symbols.")
    p.add_argument("--symbols", default=None,
                   help="Override: load only these symbols (comma-separated).")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (overrides --years).")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: today).")
    p.add_argument("--skip-etfs", action="store_true")
    p.add_argument("--skip-stocks", action="store_true")
    p.add_argument("--skip-indices", action="store_true")
    p.add_argument("--skip-crypto", action="store_true")
    p.add_argument("--out", default="data/historical_bars.db")
    args = p.parse_args(argv)

    _load_env()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    if args.start:
        start = dt.date.fromisoformat(args.start)
    else:
        start = end - dt.timedelta(days=365 * args.years)

    print(f"{'=' * 60}")
    print(f"  NSE Historical Bars Loader")
    print(f"  Window: {start} -> {end} ({(end - start).days} days)")
    print(f"  Output: {args.out}")
    print(f"{'=' * 60}")

    # If --symbols override, load only those as NSE equity
    if args.symbols:
        syms = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
        all_bars = _fetch_batch(syms, start, end, "nse_equity", "custom symbols")
    else:
        all_bars = []

        if not args.skip_etfs:
            etf_syms = tuple(s.strip().upper() for s in args.etf_symbols.split(",") if s.strip())
            all_bars.extend(_fetch_batch(etf_syms, start, end, "nse_equity", "NSE ETFs"))
            time.sleep(1)  # be polite to Yahoo

        if not args.skip_stocks:
            stock_syms = tuple(s.strip().upper() for s in args.stock_symbols.split(",") if s.strip())
            # Split large batches to avoid yfinance timeouts
            batch_size = 10
            for i in range(0, len(stock_syms), batch_size):
                batch = stock_syms[i:i + batch_size]
                all_bars.extend(_fetch_batch(batch, start, end, "nse_equity", f"NSE stocks batch {i // batch_size + 1}"))
                time.sleep(1)

        if not args.skip_indices:
            idx_syms = tuple(s.strip().upper() for s in args.index_symbols.split(",") if s.strip())
            all_bars.extend(_fetch_batch(idx_syms, start, end, "nse_equity", "NSE indices"))
            time.sleep(1)

        if not args.skip_crypto:
            crypto_syms = tuple(s.strip().upper() for s in args.crypto_symbols.split(",") if s.strip())
            all_bars.extend(_fetch_batch(crypto_syms, start, end, "crypto_inr", "Crypto INR"))

    if not all_bars:
        print("\nNo bars returned from any source.", file=sys.stderr)
        return 1

    # Write to DB
    from trading_bot.research.historical_bars import open_store, upsert_bars

    conn = open_store(Path(args.out))
    try:
        n = upsert_bars(conn, all_bars)
    finally:
        conn.close()
    print(f"\nWrote {n} rows to {args.out}.")

    # Verification report
    conn = open_store(Path(args.out))
    try:
        cur = conn.execute(
            "SELECT symbol, COUNT(*) AS n, MIN(bar_date) AS first, "
            "MAX(bar_date) AS last, source "
            "FROM bar_daily GROUP BY symbol ORDER BY symbol"
        )
        print(f"\n{'=' * 60}")
        print("  Per-symbol coverage in DB:")
        print(f"  {'symbol':<14} {'count':>6} {'first':>12} {'last':>12}  {'source'}")
        print(f"  {'-' * 56}")
        total = 0
        for row in cur.fetchall():
            print(f"  {row[0]:<14} {row[1]:>6} {row[2]:>12} {row[3]:>12}  {row[4]}")
            total += row[1]
        print(f"  {'-' * 56}")
        print(f"  {'TOTAL':<14} {total:>6}")
        print(f"{'=' * 60}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
