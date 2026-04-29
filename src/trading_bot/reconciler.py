"""Reconciler — diffs trade_journal entries against current Alpaca
positions. For each open journal entry whose symbol is no longer in
positions, look up the closing fill in Alpaca's order history and write
a closed_trades row.

Runs at 16:05 ET (post-close) and 21:55 ET (pre-digest) via cron.
On-demand via `bot reconcile`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_bot.alpaca_client import AlpacaClient
from trading_bot.reconciliation import ClosedTrade, ClosedTradeStore
from trading_bot.trade_journal import TradeJournal, TradeRecord


@dataclass(frozen=True)
class ReconcileReport:
    reconciled_count: int   # successfully wrote a closed_trades row
    unmatched_count: int    # journal entry gone from positions but no closing fill found
    errors_count: int       # exceptions during the per-symbol loop
    detail: list[dict[str, Any]]  # one entry per processed journal record


def reconcile(
    *,
    client: AlpacaClient,
    journal: TradeJournal,
    closed_trades_path: Path | str,
) -> ReconcileReport:
    """Diff trade_journal vs current Alpaca positions; write closed_trades
    for any entries whose position has disappeared. Idempotent — entries
    already in closed_trades are skipped."""
    closed_store = ClosedTradeStore(Path(closed_trades_path))
    existing_ids = {ct.entry_order_id for ct in closed_store.all()}

    open_positions = {str(p.symbol).upper().replace("/", "")
                      for p in client.get_positions()}
    journal_entries = [r for r in journal.all() if r.side.lower() == "buy"]
    # NOTE: shorts (side="sell" entry) would be the inverse — out of scope.

    reconciled = 0
    unmatched = 0
    errors = 0
    detail: list[dict[str, Any]] = []

    for entry in journal_entries:
        if entry.entry_order_id in existing_ids:
            continue
        canon_symbol = str(entry.symbol).upper().replace("/", "")
        if canon_symbol in open_positions:
            continue  # still open

        try:
            close_fill = _find_closing_fill(client, entry)
        except Exception as e:
            errors += 1
            detail.append({"symbol": entry.symbol, "outcome": "error",
                           "error": str(e)})
            continue

        if close_fill is None:
            unmatched += 1
            detail.append({"symbol": entry.symbol, "outcome": "unmatched"})
            continue

        exit_price = Decimal(str(close_fill["filled_avg_price"]))
        exit_time = close_fill["filled_at"]
        realized_pnl = (exit_price - entry.price) * entry.qty
        pnl_pct = float(realized_pnl / (entry.price * entry.qty)) if entry.price > 0 else 0.0
        # Normalize entry.timestamp for hold_hours calculation (SQLite may strip tz).
        entry_ts = entry.timestamp
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=dt.timezone.utc)
        exit_ts = exit_time
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.replace(tzinfo=dt.timezone.utc)
        hold_hours = (exit_ts - entry_ts).total_seconds() / 3600.0

        ct = ClosedTrade(
            symbol=entry.symbol, side=entry.side, qty=entry.qty,
            entry_price=entry.price, exit_price=exit_price,
            realized_pnl=realized_pnl, pnl_pct=pnl_pct,
            strategy=entry.strategy, regime=entry.regime,
            entry_time=entry.timestamp, exit_time=exit_time,
            hold_hours=hold_hours,
            entry_order_id=entry.entry_order_id,
            notes=f"reconciled: {close_fill.get('reason', 'closed')}",
        )
        closed_store.append(ct)
        reconciled += 1
        detail.append({"symbol": entry.symbol, "outcome": "reconciled",
                       "exit_price": str(exit_price)})

    return ReconcileReport(reconciled, unmatched, errors, detail)


def _find_closing_fill(client: AlpacaClient, entry: TradeRecord) -> dict | None:
    """Search Alpaca order history for a fill on `entry.symbol` after
    `entry.timestamp` whose side is opposite. Returns dict with
    filled_avg_price + filled_at + reason, or None if no match."""
    try:
        orders = client._client.get_orders()
    except Exception:
        orders = []

    opposite = "sell" if entry.side.lower() == "buy" else "buy"
    canon = str(entry.symbol).upper().replace("/", "")

    candidates = []
    for o in orders:
        if str(getattr(o, "status", "")).lower() != "filled":
            continue
        if str(getattr(o, "side", "")).lower() != opposite:
            continue
        if str(getattr(o, "symbol", "")).upper().replace("/", "") != canon:
            continue
        filled_at = getattr(o, "filled_at", None)
        if filled_at is None:
            continue
        # Normalize both datetimes to UTC-aware for comparison.
        entry_ts = entry.timestamp
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=dt.timezone.utc)
        filled_at_aware = filled_at
        if filled_at_aware.tzinfo is None:
            filled_at_aware = filled_at_aware.replace(tzinfo=dt.timezone.utc)
        if filled_at_aware < entry_ts:
            continue
        candidates.append(o)

    if not candidates:
        return None

    # Earliest closing fill after entry.
    candidates.sort(key=lambda o: o.filled_at)
    o = candidates[0]
    return {
        "filled_avg_price": o.filled_avg_price,
        "filled_at": o.filled_at,
        "reason": "stop" if str(getattr(o, "type", "")).lower().startswith("stop") else "manual",
    }
