"""Hash-chained append for ``drift_event``.

Every drift_monitor tick that produced a report appends one row per
lane to this table. The row carries the modelled vs realised slippage
comparison plus the breach flag the kernel demotes against. Append-only
so a postmortem can reconstruct WHY a lane was demoted and WHEN — the
kernel doesn't retroactively soften a breach.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


def write_event(
    conn: sqlite3.Connection,
    *,
    lane: str,
    n_trades: int,
    modelled_mean_bps: float,
    realised_mean_bps: float,
    ratio: float,
    tolerance_multiplier: float,
    breach: bool,
    recommendation: str,
    now: Optional[dt.datetime] = None,
) -> int:
    """Append one drift_event row. Returns the ledger_seq of the new row.

    The hash chain is extended every call — drift events are not
    idempotent; the same lane reporting again later is a NEW event
    (the realised_mean_bps will have moved by then anyway).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "drift_event")
    row = {
        "event_ts": now.isoformat(),
        "lane": lane,
        "n_trades": int(n_trades),
        "modelled_mean_bps": float(modelled_mean_bps),
        "realised_mean_bps": float(realised_mean_bps),
        "ratio": float(ratio),
        "tolerance_multiplier": float(tolerance_multiplier),
        "breach": 1 if breach else 0,
        "recommendation": recommendation or "",
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO drift_event (
            event_ts, lane, n_trades, modelled_mean_bps,
            realised_mean_bps, ratio, tolerance_multiplier,
            breach, recommendation, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["event_ts"], row["lane"], row["n_trades"],
         row["modelled_mean_bps"], row["realised_mean_bps"],
         row["ratio"], row["tolerance_multiplier"], row["breach"],
         row["recommendation"], prev, this_hash),
    )
    return int(cur.lastrowid)


def latest_for_lane(
    conn: sqlite3.Connection, lane: str,
) -> Optional[dict]:
    cur = conn.execute(
        "SELECT event_ts, lane, n_trades, modelled_mean_bps, "
        "realised_mean_bps, ratio, tolerance_multiplier, breach, "
        "recommendation FROM drift_event WHERE lane=? "
        "ORDER BY ledger_seq DESC LIMIT 1",
        (lane,),
    )
    r = cur.fetchone()
    if r is None:
        return None
    return {
        "event_ts": r[0], "lane": r[1], "n_trades": int(r[2]),
        "modelled_mean_bps": float(r[3]),
        "realised_mean_bps": float(r[4]),
        "ratio": float(r[5]),
        "tolerance_multiplier": float(r[6]),
        "breach": bool(r[7]),
        "recommendation": r[8] or "",
    }


__all__ = ["latest_for_lane", "write_event"]
