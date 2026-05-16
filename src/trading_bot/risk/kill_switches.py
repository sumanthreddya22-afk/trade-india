"""Kill-switch detectors + state.

Plan v4 §6 lists 8 kill switches. Any one trips → all new entries halt;
existing positions can only be reduced. This module ships:

  * The eight detector functions (each takes its inputs, returns a
    ``Kill`` event or None).
  * The ``kill_switch_event`` SQLite schema + writers (fire / clear).
  * A read-only helper ``active_kills`` that returns the current active
    detector set.

The runtime drivers for live broker error rate, data freshness, and
wall-clock skew wire in Phase 3 / Phase 5 when the corresponding
adapters / daemon ship. The detectors themselves are testable today
against fixture inputs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash

KILL_TYPES = (
    "recon_mismatch",
    "unknown_position",
    "data_freshness",
    "policy_hash_mismatch",
    "broker_api_error_rate",
    "clock_skew",
    "sqlite_integrity",
    "intraday_pnl_floor",
    # Operator-initiated halt. Not a detector — fired by the operator
    # via `bot halt` / dashboard. Cleared explicitly by `bot resume`.
    "manual_operator_halt",
)

DDL_KILL_SWITCH_EVENT = """
CREATE TABLE IF NOT EXISTS kill_switch_event (
    ledger_seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts      TEXT NOT NULL,
    detector      TEXT NOT NULL,
    event_kind    TEXT NOT NULL,                    -- fire | clear
    reason        TEXT,
    actor         TEXT NOT NULL,                    -- operator | system
    prev_hash     TEXT NOT NULL,
    this_hash     TEXT NOT NULL
);
"""

DDL_KILL_SWITCH_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS no_update_kill_switch_event
    BEFORE UPDATE ON kill_switch_event
    BEGIN
        SELECT RAISE(ABORT, 'kill_switch_event is append-only; UPDATE is forbidden by v4 §5');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS no_delete_kill_switch_event
    BEFORE DELETE ON kill_switch_event
    BEGIN
        SELECT RAISE(ABORT, 'kill_switch_event is append-only; DELETE is forbidden by v4 §5');
    END;
    """,
]


def ensure_kill_switch_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(DDL_KILL_SWITCH_EVENT)
    for stmt in DDL_KILL_SWITCH_TRIGGERS:
        cur.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------------------
# Fire / clear writers
# ---------------------------------------------------------------------------

def fire(
    conn: sqlite3.Connection,
    *,
    detector: str,
    reason: str,
    actor: str = "system",
    now: Optional[dt.datetime] = None,
) -> int:
    """Append a 'fire' row. Returns ledger_seq.

    Idempotent in spirit: callers can fire repeatedly (e.g., the same
    kill_switch loop ticking each minute); each call records a new row.
    """
    if detector not in KILL_TYPES:
        raise ValueError(f"unknown detector {detector!r}")
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "kill_switch_event")
    row = {
        "event_ts": now.isoformat(),
        "detector": detector,
        "event_kind": "fire",
        "reason": reason,
        "actor": actor,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO kill_switch_event (
            event_ts, detector, event_kind, reason, actor, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (row["event_ts"], row["detector"], row["event_kind"],
         row["reason"], row["actor"], prev, this_hash),
    )
    # Best-effort email alert. Never raises. The notifier auto-dedups
    # so a kill switch firing every 60s during a partial outage produces
    # one email, not a flood.
    try:
        from trading_bot.obs.notifier import send_kill_switch_alert
        send_kill_switch_alert(detector=detector, reason=reason, actor=actor)
    except Exception:
        pass
    return cur.lastrowid


def clear(
    conn: sqlite3.Connection,
    *,
    detector: str,
    reason: str = "",
    actor: str = "operator",
    now: Optional[dt.datetime] = None,
) -> int:
    """Append a 'clear' row. Operator notation in ``actor`` is conventional."""
    if detector not in KILL_TYPES:
        raise ValueError(f"unknown detector {detector!r}")
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "kill_switch_event")
    row = {
        "event_ts": now.isoformat(),
        "detector": detector,
        "event_kind": "clear",
        "reason": reason,
        "actor": actor,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO kill_switch_event (
            event_ts, detector, event_kind, reason, actor, prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (row["event_ts"], row["detector"], row["event_kind"],
         row["reason"], row["actor"], prev, this_hash),
    )
    return cur.lastrowid


def active_kills(conn: sqlite3.Connection) -> set[str]:
    """Return the set of detectors whose latest event is 'fire'.

    If the kill_switch_event table does not yet exist (fresh DB before
    any kill has been recorded), returns an empty set rather than
    raising — that matches the operational semantics (no recorded fires
    → no active kills).
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT detector, event_kind FROM kill_switch_event e
            WHERE ledger_seq IN (
                SELECT MAX(ledger_seq) FROM kill_switch_event GROUP BY detector
            )
            """
        )
    except sqlite3.OperationalError:
        return set()
    return {det for det, kind in cur.fetchall() if kind == "fire"}


# ---------------------------------------------------------------------------
# Detector functions (pure; take fixture inputs, return a Kill or None)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Kill:
    detector: str
    reason: str


def detect_recon_mismatch(
    *,
    latest_match: int,
    latest_window: str,
    recent_broker_switch: Optional[Mapping] = None,
) -> Optional[Kill]:
    """``match=0`` in the most recent reconciliation_proof row.

    WS6a: if ``recent_broker_switch`` is non-None (set by the caller when
    a broker_switch_event row exists within the last 24h), the kill is
    suppressed for the first window after a cutover — the ledger holds
    bot-owned positions from the OLD broker that won't appear on the
    NEW broker's account until they're sold or re-mapped.
    """
    if latest_match == 0:
        if recent_broker_switch is not None:
            return None
        return Kill(
            detector="recon_mismatch",
            reason=f"reconciliation_proof.match=0 in window={latest_window}",
        )
    return None


def detect_unknown_position(
    *,
    positions: Sequence[Mapping],
    max_age_minutes: int,
    now: dt.datetime,
) -> Optional[Kill]:
    """Any open position with classification='unknown' older than the
    threshold (default 15 min)."""
    cutoff = now - dt.timedelta(minutes=max_age_minutes)
    for p in positions:
        if p.get("classification") != "unknown":
            continue
        opened = p.get("opened_at")
        if opened is None:
            return Kill(
                detector="unknown_position",
                reason=f"symbol={p.get('symbol')} classification=unknown "
                       f"with no opened_at timestamp",
            )
        if isinstance(opened, str):
            opened = dt.datetime.fromisoformat(opened)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=dt.timezone.utc)
        if opened <= cutoff:
            return Kill(
                detector="unknown_position",
                reason=f"symbol={p.get('symbol')} classification=unknown "
                       f"for {(now-opened).total_seconds()/60:.1f}min",
            )
    return None


def detect_data_freshness(
    *,
    watermarks: Mapping[str, dt.datetime],
    thresholds_seconds: Mapping[str, int],
    now: dt.datetime,
) -> Optional[Kill]:
    """``watermarks`` maps lane name -> last data tick ts. Each lane has
    its own ``thresholds_seconds[lane]``. First breach fires the kill.
    """
    for lane, ts in watermarks.items():
        if lane not in thresholds_seconds:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        age = (now - ts).total_seconds()
        if age > thresholds_seconds[lane]:
            return Kill(
                detector="data_freshness",
                reason=f"lane={lane} stale by {age:.0f}s > "
                       f"{thresholds_seconds[lane]}s threshold",
            )
    return None


def detect_policy_hash_mismatch(
    *,
    expected: Mapping[str, str],
    actual: Mapping[str, str],
) -> Optional[Kill]:
    """``expected`` from policy/HASHES; ``actual`` recomputed from disk."""
    for path, sha in expected.items():
        if actual.get(path) != sha:
            return Kill(
                detector="policy_hash_mismatch",
                reason=f"{path}: expected {sha[:16]}..., got "
                       f"{(actual.get(path) or '?')[:16]}...",
            )
    return None


def detect_broker_api_error_rate(
    *,
    error_count: int,
    total_count: int,
    threshold_pct: float,
) -> Optional[Kill]:
    """Caller maintains a rolling window of broker API calls and passes
    counts. Detector decides if rate > threshold."""
    if total_count <= 0:
        return None
    rate_pct = error_count / total_count * 100.0
    if rate_pct > threshold_pct:
        return Kill(
            detector="broker_api_error_rate",
            reason=f"{rate_pct:.2f}% > {threshold_pct:.2f}% over "
                   f"{total_count} calls",
        )
    return None


def detect_clock_skew(
    *,
    skew_seconds: float,
    threshold_seconds: float,
) -> Optional[Kill]:
    if abs(skew_seconds) > threshold_seconds:
        return Kill(
            detector="clock_skew",
            reason=f"wall vs server skew {skew_seconds:.2f}s > "
                   f"{threshold_seconds:.2f}s",
        )
    return None


def detect_sqlite_integrity(
    integrity_check_result: str,
) -> Optional[Kill]:
    """SQLite ``PRAGMA integrity_check`` returns 'ok' on success."""
    if integrity_check_result.strip().lower() != "ok":
        return Kill(
            detector="sqlite_integrity",
            reason=f"integrity_check returned: {integrity_check_result}",
        )
    return None


def detect_intraday_pnl_floor(
    *,
    pnl_pct: float,
    floor_pct: float,
) -> Optional[Kill]:
    """``floor_pct`` is the negative threshold (e.g., -1.5)."""
    if pnl_pct <= floor_pct:
        return Kill(
            detector="intraday_pnl_floor",
            reason=f"pnl_pct {pnl_pct:.2f}% <= floor {floor_pct:.2f}%",
        )
    return None


__all__ = [
    "DDL_KILL_SWITCH_EVENT",
    "DDL_KILL_SWITCH_TRIGGERS",
    "KILL_TYPES",
    "Kill",
    "active_kills",
    "clear",
    "detect_broker_api_error_rate",
    "detect_clock_skew",
    "detect_data_freshness",
    "detect_intraday_pnl_floor",
    "detect_policy_hash_mismatch",
    "detect_recon_mismatch",
    "detect_sqlite_integrity",
    "detect_unknown_position",
    "ensure_kill_switch_table",
    "fire",
]
