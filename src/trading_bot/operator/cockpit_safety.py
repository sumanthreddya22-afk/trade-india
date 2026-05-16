"""WS5f Layer 4 — PAUSE / FLATTEN operator controls.

Three top-level operations exposed to the CLI + the cockpit:

  pause(operator, reason)    — reversible. Sets manual_halt_event state
                                to ``paused``. ``resume()`` clears it.
                                While paused, the kernel's precheck
                                halts every new entry; existing
                                positions are untouched.
  resume(operator, reason)   — clears a prior pause.
  flatten(operator, reason)  — one-way. TWAP-style exit of every
                                position over a 30-min envelope.
                                Strategies → ``observe_only``.
                                Re-enabling requires fresh paper
                                validation per the validation_policy
                                lock.

Confirmations: the cockpit + CLI front-ends are responsible for the
"type FLATTEN to confirm" anti-fat-finger guard. This module just
persists the event and computes the next allowed action.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ledger import connect_writer
from trading_bot.ledger.manual_halt_event import current_pause_state, write_event
from trading_bot.operator.controls import DEFAULT_LEDGER_PATH

log = logging.getLogger(__name__)


def _conn(ledger_db: Optional[Path] = None) -> sqlite3.Connection:
    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)
    if not ledger_db.exists():
        raise RuntimeError(f"ledger missing at {ledger_db}")
    return connect_writer(ledger_db)


def pause(
    *,
    operator: str,
    reason: str,
    source: str = "cli",
    ledger_db: Optional[Path] = None,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Record a reversible pause."""
    if not operator:
        return {"ok": False, "reason": "operator required"}
    if not reason:
        return {"ok": False, "reason": "reason required"}
    conn = _conn(ledger_db)
    try:
        if current_pause_state(conn) == "paused":
            return {"ok": True, "already_paused": True}
        write_event(
            conn, action="pause", operator=operator, source=source,
            reason=reason, now=now,
        )
        conn.commit()
        return {"ok": True, "state": "paused"}
    finally:
        conn.close()


def resume(
    *,
    operator: str,
    reason: str = "operator_resume",
    source: str = "cli",
    ledger_db: Optional[Path] = None,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Clear a prior pause. Cannot clear a flatten — flatten is one-way."""
    conn = _conn(ledger_db)
    try:
        state = current_pause_state(conn)
        if state == "flattened":
            return {
                "ok": False,
                "reason": "flattened state is one-way; cannot resume. "
                          "Re-run paper validation to re-enable.",
            }
        if state == "normal":
            return {"ok": True, "already_normal": True}
        write_event(
            conn, action="resume", operator=operator, source=source,
            reason=reason, now=now,
        )
        conn.commit()
        return {"ok": True, "state": "normal"}
    finally:
        conn.close()


def flatten(
    *,
    operator: str,
    reason: str,
    confirm_token: str,
    source: str = "cli",
    ledger_db: Optional[Path] = None,
    now: Optional[dt.datetime] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> dict:
    """One-way operator-triggered flatten.

    ``confirm_token`` must be the literal string ``"FLATTEN"`` —
    anti-fat-finger guard. Caller (CLI / cockpit) prompts for this.
    """
    if confirm_token != "FLATTEN":
        return {
            "ok": False,
            "reason": "confirm_token must be the literal 'FLATTEN'",
        }
    if not operator or not reason:
        return {"ok": False, "reason": "operator + reason required"}
    conn = _conn(ledger_db)
    try:
        write_event(
            conn, action="flatten", operator=operator, source=source,
            reason=reason, payload=payload, now=now,
        )
        # NOTE: the actual TWAP exit is enqueued by the daemon when it
        # observes the manual_halt_event row with action=flatten. This
        # function only records intent.
        conn.commit()
        return {
            "ok": True, "state": "flattened",
            "next_action": "daemon_will_twap_exit_within_30min",
        }
    finally:
        conn.close()


def state(ledger_db: Optional[Path] = None) -> dict:
    """Read the current halt state without mutating."""
    try:
        conn = _conn(ledger_db)
    except RuntimeError as e:
        return {"ok": False, "reason": str(e)}
    try:
        st = current_pause_state(conn)
        return {"ok": True, "state": st}
    finally:
        conn.close()


__all__ = ["flatten", "pause", "resume", "state"]
