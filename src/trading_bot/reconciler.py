"""Reconciler — walks Alpaca order history (source of truth) to capture
realized round-trips into closed_trades.db. Augments each row with
strategy/regime/notes from trade_journal when an entry id matches.

Runs at 16:05 ET (post-close) and 21:55 ET (pre-digest) via cron.
On-demand via `bot reconcile`.

Key behaviors:
- Pass 1 (round-trips): Pair filled buy/sell orders FIFO per symbol from
  Alpaca history. Each completed (or partially-closed) lot writes a row.
  This catches trades that were never journaled (manual orders, legacy
  paths, position-protection-driven flattens).
- Pass 2 (journal audit): For journal entries whose entry order is in a
  TERMINAL non-fill state (expired/cancelled/rejected with qty=0), write
  a 'cancelled_unfilled' audit row.
- Pending entry orders are DEFERRED — no premature $0 row. They re-evaluate
  on the next reconciler run when status finally turns terminal.
- Self-heal: at start, delete any 'reconciled_no_fill_found' or
  'cancelled_unfilled' audit row whose entry_order_id is now visibly
  FILLED on Alpaca, so Pass 1 can write the real outcome.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_bot.shared.alpaca_client import AlpacaClient
from trading_bot.reconciliation import ClosedTrade, ClosedTradeStore
from trading_bot.trade_journal import TradeJournal, TradeRecord


# Alpaca order statuses that are NOT terminal — reconciler should defer
# rather than write an audit row.
_PENDING_STATUSES = frozenset({
    "new", "accepted", "pending_new", "pending_replace", "pending_cancel",
    "partially_filled", "replaced", "held", "accepted_for_bidding",
})
_TERMINAL_NONFILL_STATUSES = frozenset({
    "expired", "cancelled", "canceled", "rejected", "suspended", "stopped",
})


@dataclass(frozen=True)
class ReconcileReport:
    reconciled_count: int   # successfully wrote a closed_trades row
    unmatched_count: int    # journal entry gone from positions but no closing fill found
    errors_count: int       # exceptions during the per-symbol loop
    detail: list[dict[str, Any]]  # one entry per processed journal record


def _normalize_status(raw: Any) -> str:
    s = str(raw or "").lower()
    if s.startswith("orderstatus."):
        s = s[len("orderstatus."):]
    return s


def _canonicalize(symbol: Any) -> str:
    return str(symbol or "").upper().replace("/", "")


def reconcile(
    *,
    client: AlpacaClient,
    journal: TradeJournal,
    closed_trades_path: Path | str,
    lookback_days: int = 30,
) -> ReconcileReport:
    """Walk Alpaca order history and journal to populate closed_trades.

    Idempotent on entry_order_id. Stale audit rows (`reconciled_no_fill_found`
    or `cancelled_unfilled`) whose entry order is now FILLED are deleted so
    a real round-trip row can take their place.
    """
    closed_store = ClosedTradeStore(Path(closed_trades_path))
    journal_entries = {r.entry_order_id: r for r in journal.all()
                       if r.side.lower() == "buy"}
    # NOTE: shorts (side="sell" entry) would be the inverse — out of scope.

    open_positions = {_canonicalize(p.symbol)
                      for p in client.get_positions()}

    all_closed_orders = _fetch_closed_orders(client, lookback_days=lookback_days)
    open_orders = _fetch_open_orders(client)
    closed_by_id = {str(getattr(o, "id", "")): o for o in all_closed_orders}
    open_by_id = {str(getattr(o, "id", "")): o for o in open_orders}

    # Self-heal: drop stale audit rows whose entry is now FILLED on Alpaca,
    # so Pass 1 can write the real round-trip.
    _purge_stale_audit_rows(closed_store, closed_by_id)

    existing_ids = {ct.entry_order_id for ct in closed_store.all()}

    reconciled = 0
    unmatched = 0
    errors = 0
    detail: list[dict[str, Any]] = []
    now_utc = dt.datetime.now(dt.timezone.utc)

    # ---- Pass 1: round-trip walker over Alpaca fills (catches non-journaled trades).
    # Seed with synthetic buy lots from journal entries whose Alpaca buy is
    # outside the lookback window — that lets a journal-only buy still pair
    # with a recent Alpaca sell.
    seed_lots = _build_journal_seed_lots(journal_entries, closed_by_id)
    round_trips = _extract_round_trips(all_closed_orders, seed_lots=seed_lots)
    for rt in round_trips:
        buy_id = rt["buy_id"]
        if buy_id in existing_ids:
            continue
        try:
            # journal_buy_id is the underlying lot id (== buy_id for first
            # close; raw lot id for subsequent partials). Use it for journal
            # lookup so partials still inherit metadata.
            jrec = journal_entries.get(rt.get("journal_buy_id", buy_id))
            ct = _build_round_trip_row(rt, jrec)
            closed_store.append(ct)
            existing_ids.add(buy_id)
            reconciled += 1
            detail.append({
                "symbol": ct.symbol, "outcome": "round_trip_captured",
                "buy_id": buy_id, "realized_pnl": str(ct.realized_pnl),
                "matched_qty": str(rt["matched_qty"]),
                "from_journal": jrec is not None,
            })
        except Exception as e:
            errors += 1
            detail.append({"symbol": rt.get("symbol"), "outcome": "round_trip_error",
                           "error": str(e)})

    # ---- Pass 2: journal audit — handle journal entries that yielded no
    # round-trip (entry expired/cancelled, or fill is genuinely missing from
    # the lookback window). Pending entries are deferred.
    for entry in journal_entries.values():
        if entry.entry_order_id in existing_ids:
            continue
        if _canonicalize(entry.symbol) in open_positions:
            continue  # currently held — wait for the exit

        try:
            outcome = _classify_journal_entry(
                entry, closed_by_id=closed_by_id, open_by_id=open_by_id,
            )
        except Exception as e:
            errors += 1
            detail.append({"symbol": entry.symbol, "outcome": "audit_error",
                           "error": str(e)})
            continue

        if outcome == "deferred_pending":
            detail.append({"symbol": entry.symbol, "outcome": "deferred_pending"})
            continue

        if outcome in ("cancelled_unfilled", "reconciled_no_fill_found"):
            ct = _build_audit_row(entry, outcome=outcome, now_utc=now_utc)
            closed_store.append(ct)
            existing_ids.add(entry.entry_order_id)
            reconciled += 1
            detail.append({"symbol": entry.symbol, "outcome": outcome})
            continue

        # outcome == "filled_no_exit_match": the entry filled but no closing
        # sell could be paired in the lookback window (and the symbol isn't
        # currently open). Edge case — surface for visibility, don't write.
        unmatched += 1
        detail.append({"symbol": entry.symbol, "outcome": "filled_no_exit_match"})

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


def _fetch_open_orders(client: AlpacaClient) -> list:
    """Fetch currently OPEN orders so the journal-audit pass can detect
    pending (not-yet-filled) entries and defer rather than write a $0 row."""
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        return client._client.get_orders(filter=req)
    except Exception:
        return []


def _purge_stale_audit_rows(closed_store: ClosedTradeStore,
                             closed_by_id: dict) -> int:
    """Delete audit rows ('reconciled_no_fill_found' / 'cancelled_unfilled')
    whose entry_order_id is now visibly FILLED on Alpaca. Lets Pass 1 write
    the real outcome.

    Why this exists: the previous reconciler implementation wrote a $0 audit
    row whenever the entry order was missing from the CLOSED-orders fetch,
    even when it was simply still pending (e.g. an off-hours stock buy).
    Once the order eventually filled, the stale row blocked any update
    because ClosedTradeStore.append is idempotent on entry_order_id.
    """
    purged = 0
    for ct in closed_store.all():
        notes = (ct.notes or "").lower()
        if not ("reconciled_no_fill_found" in notes
                or "cancelled_unfilled" in notes):
            continue
        order = closed_by_id.get(ct.entry_order_id)
        if order is None:
            continue
        status = _normalize_status(getattr(order, "status", ""))
        if status != "filled":
            continue
        try:
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        if filled_qty <= 0.0:
            continue
        purged += closed_store.delete_by_entry_order_id(ct.entry_order_id)
    return purged


def _build_journal_seed_lots(
    journal_entries: dict[str, TradeRecord],
    closed_by_id: dict,
) -> list[dict]:
    """For each journal BUY whose Alpaca buy fill is missing from the
    closed-orders fetch (retention window, manual order with no Alpaca echo),
    fabricate a synthetic buy lot so the round-trip walker can pair the
    journal buy with an Alpaca sell that's still in the lookback.

    Skips entries whose Alpaca order IS visible — the real fill is the
    authoritative buy lot, and using both would double-count.
    """
    seeds: list[dict] = []
    for entry in journal_entries.values():
        if entry.side.lower() != "buy":
            continue
        order = closed_by_id.get(entry.entry_order_id)
        if order is not None:
            status = _normalize_status(getattr(order, "status", ""))
            filled_qty = _safe_float(getattr(order, "filled_qty", 0))
            if status == "filled" and filled_qty > 0:
                continue  # real Alpaca buy fill exists; walker will use it
        seeds.append({
            "buy_id": entry.entry_order_id,
            "symbol": entry.symbol,
            "price": entry.price,
            "time": _ensure_utc(entry.timestamp),
            "qty_remaining": entry.qty,
            "close_count": 0,
        })
    return seeds


def _extract_round_trips(all_orders: list,
                          *, seed_lots: list[dict] | None = None) -> list[dict]:
    """Pair filled buy/sell orders into completed round-trips, FIFO per
    canonical symbol. ``seed_lots`` are pre-existing buy lots (typically
    fabricated from journal entries whose Alpaca buy is outside the
    lookback window). Each emitted dict represents one closing event:

      {
        "symbol":         <original symbol from buy fill, e.g. "FIL/USD">,
        "buy_id":         <buy order's Alpaca id>,
        "buy_price":      Decimal,
        "buy_time":       datetime (utc),
        "exit_price":     Decimal,           # this sell event's avg price
        "exit_time":      datetime (utc),    # this sell event's filled_at
        "matched_qty":    Decimal,           # qty of the buy lot closed by this sell
        "realized_pnl":   Decimal,
        "exit_reason":    "stop" | "manual",
      }

    A buy lot can be closed across multiple sell events. Only the FIRST
    closing event for a lot uses the buy_id as the entry_order_id (so
    journal lookup and decision_lessons join continue to work). Subsequent
    closes against the same lot get composite ids `{buy_id}::{sell_id}`.
    """
    fills = [o for o in all_orders
             if _normalize_status(getattr(o, "status", "")) == "filled"
             and _safe_float(getattr(o, "filled_qty", 0)) > 0
             and getattr(o, "filled_at", None) is not None
             and getattr(o, "filled_avg_price", None) is not None]

    # Build a unified, time-ordered event stream so synthetic journal seeds
    # take their proper place in FIFO order alongside Alpaca fills.
    events: list[tuple[dt.datetime, int, str, Any]] = []
    # 2nd tuple element is a tiebreaker so seeds come before fills with the
    # same timestamp (a journal record at T should be a "lot" before a sell
    # at T can consume it).
    for lot in (seed_lots or []):
        events.append((_ensure_utc(lot["time"]), 0, "seed", lot))
    for o in fills:
        events.append((_ensure_utc(o.filled_at), 1, "fill", o))
    events.sort(key=lambda e: (e[0], e[1]))

    # Per-symbol FIFO of open buy lots.
    lots_by_symbol: "defaultdict[str, deque[dict]]" = defaultdict(deque)
    round_trips: list[dict] = []

    for _ts, _tie, kind, payload in events:
        if kind == "seed":
            sym = _canonicalize(payload["symbol"])
            lots_by_symbol[sym].append(dict(payload))  # copy: walker mutates
            continue
        # kind == "fill"
        o = payload
        sym = _canonicalize(getattr(o, "symbol", ""))
        side = str(getattr(o, "side", "")).lower()
        try:
            qty = Decimal(str(o.filled_qty))
            price = Decimal(str(o.filled_avg_price))
        except Exception:
            continue
        ts = _ensure_utc(o.filled_at)

        if "buy" in side:
            lots_by_symbol[sym].append({
                "buy_id": str(getattr(o, "id", "")),
                "symbol": str(getattr(o, "symbol", "")),
                "price": price,
                "time": ts,
                "qty_remaining": qty,
                "close_count": 0,
            })
            continue
        if "sell" not in side:
            continue

        sell_remaining = qty
        sell_price = price
        sell_time = ts
        sell_id = str(getattr(o, "id", ""))
        order_type = str(getattr(o, "type", "")).lower()
        exit_reason = "stop" if "stop" in order_type else "manual"

        lots = lots_by_symbol[sym]
        while sell_remaining > 0 and lots:
            lot = lots[0]
            if lot["qty_remaining"] <= 0:
                lots.popleft()
                continue
            matched = min(sell_remaining, lot["qty_remaining"])
            realized_pnl = (sell_price - lot["price"]) * matched
            entry_id_for_row = (
                lot["buy_id"] if lot["close_count"] == 0
                else f"{lot['buy_id']}::{sell_id}"
            )
            round_trips.append({
                "symbol": lot["symbol"],
                "buy_id": entry_id_for_row,
                "journal_buy_id": lot["buy_id"],
                "buy_price": lot["price"],
                "buy_time": lot["time"],
                "exit_price": sell_price,
                "exit_time": sell_time,
                "matched_qty": matched,
                "realized_pnl": realized_pnl,
                "exit_reason": exit_reason,
            })
            lot["qty_remaining"] -= matched
            lot["close_count"] += 1
            sell_remaining -= matched
            if lot["qty_remaining"] <= 0:
                lots.popleft()
        # If sell_remaining > 0: orphan sell (no prior buy in lookback) — ignore.

    return round_trips


def _build_round_trip_row(rt: dict, jrec: TradeRecord | None) -> ClosedTrade:
    """Build a ClosedTrade from a round-trip dict, augmenting metadata from
    the trade journal when an entry id matches."""
    matched_qty = rt["matched_qty"]
    buy_price = rt["buy_price"]
    exit_price = rt["exit_price"]
    realized_pnl = rt["realized_pnl"]
    pnl_pct = (
        float(realized_pnl / (buy_price * matched_qty))
        if buy_price > 0 and matched_qty > 0 else 0.0
    )
    buy_time = _ensure_utc(rt["buy_time"])
    exit_time = _ensure_utc(rt["exit_time"])
    hold_hours = (exit_time - buy_time).total_seconds() / 3600.0

    if jrec is not None:
        symbol = jrec.symbol
        side = jrec.side
        strategy = jrec.strategy
        regime = jrec.regime
    else:
        symbol = rt["symbol"]
        side = "buy"
        strategy = "external"
        regime = "unknown"

    return ClosedTrade(
        symbol=symbol, side=side, qty=matched_qty,
        entry_price=buy_price, exit_price=exit_price,
        realized_pnl=realized_pnl, pnl_pct=pnl_pct,
        strategy=strategy, regime=regime,
        entry_time=buy_time, exit_time=exit_time,
        hold_hours=hold_hours,
        entry_order_id=rt["buy_id"],
        notes=f"reconciled: {rt['exit_reason']}",
    )


def _build_audit_row(entry: TradeRecord, *, outcome: str,
                     now_utc: dt.datetime) -> ClosedTrade:
    entry_ts = _ensure_utc(entry.timestamp)
    hold_hours = (now_utc - entry_ts).total_seconds() / 3600.0
    return ClosedTrade(
        symbol=entry.symbol, side=entry.side, qty=entry.qty,
        entry_price=entry.price, exit_price=entry.price,
        realized_pnl=Decimal("0"), pnl_pct=0.0,
        strategy=entry.strategy, regime=entry.regime,
        entry_time=entry.timestamp, exit_time=now_utc,
        hold_hours=hold_hours,
        entry_order_id=entry.entry_order_id,
        notes=f"{outcome}: entry_order_id={entry.entry_order_id}",
    )


def _classify_journal_entry(
    entry: TradeRecord, *, closed_by_id: dict, open_by_id: dict,
) -> str:
    """Classify a journal entry with no current open position and no Pass-1
    round-trip. Returns one of:

      - "deferred_pending"           → entry order is still pending; do nothing
      - "cancelled_unfilled"         → entry order is terminal with 0 fill
      - "reconciled_no_fill_found"   → entry order is missing from Alpaca
                                       (retention / lookback)
      - "filled_no_exit_match"       → entry filled but no exit found in
                                       lookback window (rare edge case)

    Drives off the order's STATUS rather than which list it appeared in,
    because Alpaca occasionally surfaces the same order in both lists during
    state transitions, and tests sometimes share a single mock return value.
    """
    order = open_by_id.get(entry.entry_order_id) \
        or closed_by_id.get(entry.entry_order_id)
    if order is None:
        return "reconciled_no_fill_found"

    status = _normalize_status(getattr(order, "status", ""))
    if status in _PENDING_STATUSES:
        return "deferred_pending"

    if status in _TERMINAL_NONFILL_STATUSES:
        filled_qty = _safe_float(getattr(order, "filled_qty", 0))
        if filled_qty == 0.0:
            return "cancelled_unfilled"
        # Partial fill then cancel — Pass 1 should have paired any matching
        # sell. Fall through.
    return "filled_no_exit_match"


def _safe_float(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Legacy helper kept for backward compat with any external callers.
# ---------------------------------------------------------------------------

def _find_closing_fill(
    client: AlpacaClient,
    entry: TradeRecord,
    *,
    lookback_days: int = 30,
) -> dict | None:
    """Search Alpaca order history for a closing sell fill against ``entry``.
    Returns dict with filled_avg_price + filled_at + reason, or None.

    Kept for any external callers that imported this. Internally, the
    reconciler now uses _extract_round_trips() instead.
    """
    orders = _fetch_closed_orders(client, lookback_days=lookback_days)
    rts = _extract_round_trips(orders)
    canon = _canonicalize(entry.symbol)
    for rt in rts:
        if rt["journal_buy_id"] != entry.entry_order_id:
            continue
        if _canonicalize(rt["symbol"]) != canon:
            continue
        return {
            "filled_avg_price": rt["exit_price"],
            "filled_at": rt["exit_time"],
            "reason": rt["exit_reason"],
        }
    return None


# ---------------------------------------------------------------------------
# Wheel option reconciliation — option-fill pass.
# ---------------------------------------------------------------------------

import datetime as _dt
from decimal import Decimal as _Decimal

from sqlalchemy import select as _select
from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session

from trading_bot.alerts import AlertEvent as _AlertEvent
from trading_bot.options.wheel_state import (
    Phase as _Phase, WheelStateRepo as _WheelStateRepo,
    close_cycle as _close_cycle, mark_assigned as _mark_assigned,
)
from trading_bot.state_db import OptionFill as _OptionFill, WheelCycle as _WheelCycle


def reconcile_options(
    *, engine: _Engine, option_alpaca, alpaca_equity, alert_queue,
) -> None:
    """For each open wheel cycle whose short option no longer appears in
    Alpaca's option positions, classify the outcome:
      - underlying now shows +100 shares per contract → CSP assigned
      - underlying still flat, near/past expiration → expired worthless or BTC fill
      - CC: underlying drops to 0 shares → called away
    Emits the matching wheel_* alert."""
    repo = _WheelStateRepo(engine)
    open_option_symbols = {str(p.symbol) for p in option_alpaca.get_option_positions()}
    eq_positions = {str(p.symbol): p for p in alpaca_equity.get_positions()}

    for cyc in repo.list_active():
        contract = cyc.cc_contract or cyc.csp_contract
        if contract is None or contract in open_option_symbols:
            continue  # still open

        is_cc = (cyc.phase == _Phase.CC_OPEN.value)
        eq = eq_positions.get(cyc.symbol)
        eq_qty = int(_Decimal(str(getattr(eq, "qty", "0") or "0"))) if eq else 0

        if not is_cc:
            # CSP closed somehow
            if eq_qty >= 100:
                _mark_assigned(repo, cycle_id=cyc.cycle_id,
                               when=_dt.datetime.now(_dt.timezone.utc))
                alert_queue(_AlertEvent(
                    kind="wheel_assignment", severity="warn",
                    title=f"CSP assigned: {cyc.symbol} @ {cyc.csp_strike}",
                    detail_html=f"<p>{cyc.symbol} now holding {eq_qty} shares</p>",
                    fired_at=_dt.datetime.now(_dt.timezone.utc),
                    dedup_key=f"assignment_{cyc.cycle_id}",
                ))
            else:
                # Expired worthless or already bought-to-close
                pnl = (cyc.csp_credit or _Decimal(0)) * _Decimal(100)
                _close_cycle(repo, cycle_id=cyc.cycle_id, realized_pnl=pnl)
        else:
            # CC closed somehow
            if eq_qty == 0:
                # called away
                pnl = ((cyc.csp_credit or _Decimal(0)) + (cyc.cc_credit or _Decimal(0))) \
                      * _Decimal(100) + ((cyc.cc_strike or _Decimal(0))
                                          - (cyc.cost_basis or _Decimal(0))) * _Decimal(100)
                _close_cycle(repo, cycle_id=cyc.cycle_id, realized_pnl=pnl)
                alert_queue(_AlertEvent(
                    kind="wheel_called_away", severity="info",
                    title=f"CC called away: {cyc.symbol} @ {cyc.cc_strike}",
                    detail_html=f"<p>{cyc.symbol} called away — cycle closed</p>",
                    fired_at=_dt.datetime.now(_dt.timezone.utc),
                    dedup_key=f"called_{cyc.cycle_id}",
                ))
            else:
                # CC expired worthless — cycle reverts back to assigned (still hold shares)
                with _Session(engine) as s:
                    row = s.query(_WheelCycle).filter(_WheelCycle.cycle_id == cyc.cycle_id).one()
                    row.phase = _Phase.ASSIGNED.value
                    row.cc_contract = None
                    row.cc_strike = None
                    row.cc_expiration = None
                    row.cc_credit = None
                    s.commit()
