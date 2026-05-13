"""Corporate-action ingest + cross-check.

Plan v4 §9 + §1B: splits / dividends / mergers / spinoffs must be
normalised before features depending on the affected symbol can be
computed. Two sources (e.g., Alpaca corporate-actions API + a secondary
like yfinance) are cross-checked nightly; mismatch halts the affected
lane (Phase 5 daemon wires the halt).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping, Optional, Sequence

from trading_bot.ingest.schema import ensure_ingest_tables
from trading_bot.ledger.hash_chain import compute_this_hash, last_hash

ActionTypeT = Literal["split", "dividend", "merger", "spinoff"]


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    action_type: ActionTypeT
    ex_date: dt.date
    factor: float                    # split ratio (e.g., 2.0 for 2-for-1); dividend amount in $
    source_id: str
    raw_payload: Mapping
    """The original payload from the source — hashed into ``raw_payload_hash``."""


@dataclass(frozen=True)
class CrossCheckResult:
    symbol: str
    ex_date: dt.date
    action_type: ActionTypeT
    match: bool
    sources: tuple[str, ...]                # sources that reported it
    factors: dict[str, float]               # source_id -> factor
    note: str = ""


def record_action(
    conn: sqlite3.Connection,
    action: CorporateAction,
    *,
    now: Optional[dt.datetime] = None,
) -> int:
    """Append one corporate_action row. Hash-chained.

    UNIQUE (symbol, action_type, ex_date, source_id) makes re-ingest
    idempotent — IntegrityError on second insert.
    """
    ensure_ingest_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "corporate_action")
    raw = json.dumps(action.raw_payload, sort_keys=True,
                     separators=(",", ":"), default=str)
    payload_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    row = {
        "event_ts": now.isoformat(),
        "symbol": action.symbol,
        "action_type": action.action_type,
        "ex_date": action.ex_date.isoformat(),
        "factor": float(action.factor),
        "source_id": action.source_id,
        "raw_payload_hash": payload_hash,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO corporate_action (
            event_ts, symbol, action_type, ex_date, factor, source_id,
            raw_payload_hash, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            row["event_ts"], row["symbol"], row["action_type"],
            row["ex_date"], row["factor"], row["source_id"],
            row["raw_payload_hash"], prev, this_hash,
        ),
    )
    return cur.lastrowid


def cross_check(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    action_type: ActionTypeT,
    ex_date: dt.date,
    factor_tolerance_rel: float = 1e-4,
) -> CrossCheckResult:
    """Compare all rows for one (symbol, type, date) across sources.

    ``factor_tolerance_rel`` is the relative tolerance for considering
    two factors equal (1e-4 = 0.01% — handles floating point noise on
    fractional splits / dividends).
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT source_id, factor FROM corporate_action "
            "WHERE symbol=? AND action_type=? AND ex_date=?",
            (symbol, action_type, ex_date.isoformat()),
        )
    except sqlite3.OperationalError:
        return CrossCheckResult(
            symbol=symbol, ex_date=ex_date, action_type=action_type,
            match=False, sources=(), factors={},
            note="corporate_action table not yet created",
        )
    rows = cur.fetchall()
    if not rows:
        return CrossCheckResult(
            symbol=symbol, ex_date=ex_date, action_type=action_type,
            match=False, sources=(), factors={},
            note="no rows recorded",
        )
    if len(rows) == 1:
        src, factor = rows[0]
        return CrossCheckResult(
            symbol=symbol, ex_date=ex_date, action_type=action_type,
            match=False, sources=(src,), factors={src: float(factor)},
            note="only one source has recorded this action",
        )
    factors = {src: float(f) for src, f in rows}
    f_values = list(factors.values())
    f0 = f_values[0]
    if f0 == 0:
        match = all(v == 0 for v in f_values)
    else:
        match = all(abs(v - f0) / abs(f0) <= factor_tolerance_rel for v in f_values)
    return CrossCheckResult(
        symbol=symbol, ex_date=ex_date, action_type=action_type,
        match=match, sources=tuple(factors.keys()), factors=factors,
        note="match" if match else "factor mismatch across sources",
    )


# ---------------------------------------------------------------------------
# Pure-math helpers for backtest + reconciliation
# ---------------------------------------------------------------------------

def apply_split_to_qty(qty: float, factor: float) -> float:
    """A 2-for-1 split doubles the share count: new = old * factor."""
    return qty * factor


def apply_split_to_price(price: float, factor: float) -> float:
    """A 2-for-1 split halves the price: new = old / factor."""
    if factor == 0:
        return price
    return price / factor


def apply_dividend_to_cash(qty: float, dividend_per_share: float) -> float:
    """Cash credited on dividend = shares_held × per-share amount."""
    return qty * dividend_per_share


__all__ = [
    "ActionTypeT",
    "CorporateAction",
    "CrossCheckResult",
    "apply_dividend_to_cash",
    "apply_split_to_price",
    "apply_split_to_qty",
    "cross_check",
    "record_action",
]
