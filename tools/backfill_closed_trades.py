"""One-shot backfill — repopulate data/closed_trades.db from Alpaca's
recent order history.

Why this exists: until the reconciler fix landed, two failure modes left
realized P&L invisible in closed_trades.db:
  1. Off-hours stock entries got a $0 'reconciled_no_fill_found' audit row
     written by the 21:55 ET reconciler before the order had filled. The
     row was sticky (idempotent on entry_order_id), so even after the order
     filled the next day, no real outcome ever made it to closed_trades.
  2. Trades placed outside the journal-driven orchestrator path (manual
     orders, position-protection-driven flattens, legacy code) never had
     a journal row, so the journal-driven reconciler never visited them.

The new reconciler walks Alpaca order history directly. This script just
runs that reconciler over the configured DB so historical fills
(FIL/USD round-trips, etc.) get captured immediately.

Usage:
    .venv/bin/python tools/backfill_closed_trades.py [--lookback-days 30]
                                                     [--dry-run]
                                                     [--db data/closed_trades.db]
                                                     [--journal data/trade_journal.db]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--db", type=Path,
                        default=Path("data/closed_trades.db"))
    parser.add_argument("--journal", type=Path,
                        default=Path("data/trade_journal.db"))
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change without writing")
    args = parser.parse_args()

    from trading_bot.alpaca_client import AlpacaClient
    from trading_bot.config import Settings
    from trading_bot.reconciliation import ClosedTradeStore
    from trading_bot.reconciler import reconcile
    from trading_bot.trade_journal import TradeJournal

    settings = Settings()
    client = AlpacaClient(settings)
    journal = TradeJournal(args.journal)

    if args.dry_run:
        # Run the reconciler against an in-memory DB and report deltas.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_db = Path(tmp) / "closed.db"
            # Seed with current state so the diff is meaningful.
            real_store = ClosedTradeStore(args.db)
            tmp_store = ClosedTradeStore(tmp_db)
            for ct in real_store.all():
                tmp_store.append(ct)
            report = reconcile(client=client, journal=journal,
                               closed_trades_path=tmp_db,
                               lookback_days=args.lookback_days)
            print(json.dumps({
                "dry_run": True,
                "reconciled_count": report.reconciled_count,
                "unmatched_count": report.unmatched_count,
                "errors_count": report.errors_count,
                "detail": report.detail,
            }, default=str, indent=2))
        return 0

    report = reconcile(client=client, journal=journal,
                       closed_trades_path=args.db,
                       lookback_days=args.lookback_days)
    print(json.dumps({
        "reconciled_count": report.reconciled_count,
        "unmatched_count": report.unmatched_count,
        "errors_count": report.errors_count,
        "detail": report.detail,
    }, default=str, indent=2))

    # Print final state
    store = ClosedTradeStore(args.db)
    rows = store.all()
    realized = sum((r.realized_pnl for r in rows),
                   start=__import__("decimal").Decimal("0"))
    print(f"\nclosed_trades.db now has {len(rows)} rows; "
          f"realized P&L total = {realized}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
