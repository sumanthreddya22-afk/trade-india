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
    lookback_days: int = 30,
) -> ReconcileReport:
    """Diff trade_journal vs current Alpaca positions; write closed_trades
    for any entries whose position has disappeared. Idempotent — entries
    already in closed_trades are skipped.

    Three outcomes per journal entry (after deduplication check):
    1. Still open in Alpaca positions → skip.
    2. Closing sell fill found → write real closed_trades row.
    3. Entry order was EXPIRED/CANCELLED (never filled) or no fill found
       at all → write audit-fallback row with exit_price=entry_price,
       realized_pnl=0, notes="cancelled_unfilled" or
       "reconciled_no_fill_found". This makes future runs idempotent.
    """
    closed_store = ClosedTradeStore(Path(closed_trades_path))
    existing_ids = {ct.entry_order_id for ct in closed_store.all()}

    open_positions = {str(p.symbol).upper().replace("/", "")
                      for p in client.get_positions()}
    journal_entries = [r for r in journal.all() if r.side.lower() == "buy"]
    # NOTE: shorts (side="sell" entry) would be the inverse — out of scope.

    # Fetch closed orders once with a generous lookback (avoids N Alpaca calls).
    all_closed_orders = _fetch_closed_orders(client, lookback_days=lookback_days)
    # Index by order-id string for fast lookup.
    orders_by_id = {str(getattr(o, "id", "")): o for o in all_closed_orders}

    reconciled = 0
    unmatched = 0
    errors = 0
    detail: list[dict[str, Any]] = []

    now_utc = dt.datetime.now(dt.timezone.utc)

    for entry in journal_entries:
        if entry.entry_order_id in existing_ids:
            continue
        canon_symbol = str(entry.symbol).upper().replace("/", "")
        if canon_symbol in open_positions:
            continue  # still open

        try:
            result = _classify_entry(entry, all_closed_orders, orders_by_id)
        except Exception as e:
            errors += 1
            detail.append({"symbol": entry.symbol, "outcome": "error",
                           "error": str(e)})
            continue

        if result["outcome"] == "filled":
            close_fill = result["fill"]
            exit_price = Decimal(str(close_fill["filled_avg_price"]))
            exit_time = close_fill["filled_at"]
            realized_pnl = (exit_price - entry.price) * entry.qty
            pnl_pct = float(realized_pnl / (entry.price * entry.qty)) if entry.price > 0 else 0.0
            entry_ts = _ensure_utc(entry.timestamp)
            exit_ts = _ensure_utc(exit_time)
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

        elif result["outcome"] in ("cancelled_unfilled", "reconciled_no_fill_found"):
            # Entry order never filled or Alpaca has no record — write audit row
            # so this entry is never processed again.
            exit_reason = result["outcome"]
            entry_ts = _ensure_utc(entry.timestamp)
            hold_hours = (now_utc - entry_ts).total_seconds() / 3600.0

            ct = ClosedTrade(
                symbol=entry.symbol, side=entry.side, qty=entry.qty,
                entry_price=entry.price, exit_price=entry.price,
                realized_pnl=Decimal("0"), pnl_pct=0.0,
                strategy=entry.strategy, regime=entry.regime,
                entry_time=entry.timestamp, exit_time=now_utc,
                hold_hours=hold_hours,
                entry_order_id=entry.entry_order_id,
                notes=(
                    f"{exit_reason}: entry_order_id={entry.entry_order_id}"
                ),
            )
            closed_store.append(ct)
            reconciled += 1
            detail.append({"symbol": entry.symbol, "outcome": exit_reason})

        else:
            # outcome == "unmatched" (fill search returned nothing and no
            # order record found at all — should be rare after lookback fix).
            unmatched += 1
            detail.append({"symbol": entry.symbol, "outcome": "unmatched"})

    return ReconcileReport(reconciled, unmatched, errors, detail)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_utc(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts


def _fetch_closed_orders(client: AlpacaClient, *, lookback_days: int = 30) -> list:
    """Fetch all closed orders from Alpaca with a generous lookback window."""
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=500,
            after=after,
        )
        return client._client.get_orders(filter=req)
    except Exception:
        return []


def _classify_entry(
    entry: TradeRecord,
    all_orders: list,
    orders_by_id: dict,
) -> dict:
    """Classify a journal entry with no open position into one of:
    - "filled"               → closing sell fill found; fill dict attached
    - "cancelled_unfilled"   → entry order exists but expired/cancelled with no fill
    - "reconciled_no_fill_found" → no Alpaca order found at all (retention limit)
    - "unmatched"            → (legacy path, should not occur with current logic)
    """
    # 1. Check if the entry order itself is present (expired / cancelled).
    entry_order = orders_by_id.get(entry.entry_order_id)
    if entry_order is not None:
        status = str(getattr(entry_order, "status", "")).lower()
        # Strip "orderstatus." prefix if present.
        if status.startswith("orderstatus."):
            status = status[len("orderstatus."):]
        if status in ("expired", "cancelled", "canceled"):
            filled_qty = getattr(entry_order, "filled_qty", None)
            # Treat as unfilled if qty is None, "0", or 0.
            try:
                qty_val = float(filled_qty) if filled_qty is not None else 0.0
            except (TypeError, ValueError):
                qty_val = 0.0
            if qty_val == 0.0:
                return {"outcome": "cancelled_unfilled"}
            # Partially filled — fall through to find a closing fill.

    # 2. Search for a closing sell fill after the entry.
    opposite = "sell" if entry.side.lower() == "buy" else "buy"
    canon = str(entry.symbol).upper().replace("/", "")

    candidates = []
    for o in all_orders:
        if str(getattr(o, "status", "")).lower() not in ("filled", "orderstatus.filled"):
            continue
        o_side = str(getattr(o, "side", "")).lower()
        if "buy" in o_side and opposite == "buy":
            pass
        elif "sell" in o_side and opposite == "sell":
            pass
        else:
            continue
        if str(getattr(o, "symbol", "")).upper().replace("/", "") != canon:
            continue
        filled_at = getattr(o, "filled_at", None)
        if filled_at is None:
            continue
        entry_ts = _ensure_utc(entry.timestamp)
        filled_at_aware = _ensure_utc(filled_at)
        if filled_at_aware < entry_ts:
            continue
        candidates.append(o)

    if candidates:
        candidates.sort(key=lambda o: o.filled_at)
        o = candidates[0]
        order_type = str(getattr(o, "type", "")).lower()
        return {
            "outcome": "filled",
            "fill": {
                "filled_avg_price": o.filled_avg_price,
                "filled_at": o.filled_at,
                "reason": "stop" if "stop" in order_type else "manual",
            },
        }

    # 3. No fill found — if we had no order record at all, it's a retention issue.
    if entry_order is None:
        return {"outcome": "reconciled_no_fill_found"}

    # 4. Order exists but status was something other than expired/cancelled/filled
    #    (e.g., a partially filled entry with no matching exit).
    return {"outcome": "unmatched"}


# ---------------------------------------------------------------------------
# Legacy helper kept for backward compat with any external callers.
# ---------------------------------------------------------------------------

def _find_closing_fill(
    client: AlpacaClient,
    entry: TradeRecord,
    *,
    lookback_days: int = 30,
) -> dict | None:
    """Search Alpaca order history for a fill on ``entry.symbol`` after
    ``entry.timestamp`` whose side is opposite. Returns dict with
    filled_avg_price + filled_at + reason, or None if no match.

    Prefer calling _classify_entry() directly for new code — this wrapper
    exists for backward compatibility.
    """
    orders = _fetch_closed_orders(client, lookback_days=lookback_days)
    orders_by_id = {str(getattr(o, "id", "")): o for o in orders}
    result = _classify_entry(entry, orders, orders_by_id)
    if result["outcome"] == "filled":
        return result["fill"]
    return None
