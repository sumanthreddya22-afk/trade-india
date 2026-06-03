#!/usr/bin/env python
"""Reset the paper trading portfolio to start fresh.

Updates data/paper_portfolio.json with a new inception date and
starting equity. All P&L tracking starts from this date forward.

Usage:
  python tools/reset_paper_portfolio.py                     # starts tomorrow
  python tools/reset_paper_portfolio.py --date 2026-06-10   # specific date
  python tools/reset_paper_portfolio.py --equity 500000     # INR 5 lakh per strategy
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

CONFIG_PATH = Path("data/paper_portfolio.json")


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--date", default=None,
                   help="Inception date YYYY-MM-DD (default: tomorrow)")
    p.add_argument("--equity", type=int, default=10_00_000,
                   help="Starting equity per strategy in INR (default: 10,00,000)")
    args = p.parse_args(argv)

    inception = (
        dt.date.fromisoformat(args.date)
        if args.date
        else dt.date.today() + dt.timedelta(days=1)
    )

    # Read existing config to preserve strategy list
    if CONFIG_PATH.exists():
        existing = json.loads(CONFIG_PATH.read_text())
        strategies = existing.get("strategies", [])
    else:
        strategies = [
            {
                "id": "etf_momentum", "name": "ETF Momentum",
                "universe": ["NIFTYBEES", "JUNIORBEES", "BANKBEES",
                             "GOLDBEES", "LIQUIDBEES", "SETFNIF50"],
                "lane": "stocks",
                "signal_module": "trading_bot.strategies.etf_momentum_v1.signal",
                "rebalance_freq": "monthly",
            },
            {
                "id": "dual_momentum", "name": "Dual Momentum",
                "universe": ["NIFTYBEES", "LIQUIDBEES"],
                "lane": "stocks",
                "signal_module": "trading_bot.strategies.dual_momentum_v1.signal",
                "rebalance_freq": "monthly",
            },
            {
                "id": "crypto_momentum", "name": "Crypto Momentum",
                "universe": ["BTC/INR", "ETH/INR"],
                "lane": "crypto",
                "signal_module": "trading_bot.strategies.crypto_momentum_v1.signal",
                "rebalance_freq": "monthly",
            },
        ]

    config = {
        "inception_date": inception.isoformat(),
        "starting_equity_per_strategy": args.equity,
        "currency": "INR",
        "note": f"Reset by operator on {dt.date.today().isoformat()}. "
                f"All P&L tracks from {inception.isoformat()} forward.",
        "strategies": strategies,
    }

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")

    print(f"Portfolio reset!")
    print(f"  Inception date:    {inception}")
    print(f"  Equity/strategy:   INR {args.equity:,.0f}")
    print(f"  Strategies:        {len(strategies)}")
    print(f"  Config written to: {CONFIG_PATH}")
    print(f"\nDashboard: http://localhost:8765/portfolio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
