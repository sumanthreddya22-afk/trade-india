"""Daily-digest helper — what happened in the last N hours.

Surfaces:
  * Account: opening vs latest equity, intraday P&L, equity curve points.
  * Heartbeats: which jobs ticked, which errored, which were skipped.
  * Kill switches: every fire/clear, with reason.
  * Strategy submissions: anything new the operator registered.
  * Orders: new orders + fills (when wired).

Returns a structured dict the CLI pretty-prints and the dashboard
renders as a table. Default window = 24h, configurable.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Optional

from trading_bot.ledger import DEFAULT_LEDGER_PATH


def build_digest(
    *, hours: int = 24, ledger_db: Optional[Path] = None,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    ledger_db = ledger_db or (Path.cwd() / DEFAULT_LEDGER_PATH)
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=hours)
    out: dict[str, Any] = {
        "ts": now.isoformat(),
        "window_hours": hours,
        "since": since.isoformat(),
        "ledger_db": str(ledger_db),
        "ledger_present": ledger_db.exists(),
    }
    if not ledger_db.exists():
        out["error"] = "ledger missing"
        return out

    conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
    try:
        out["account"] = _account_summary(conn, since=since)
        out["heartbeats"] = _heartbeats(conn)
        out["kill_switches"] = _kill_switches(conn, since=since)
        out["strategy_submissions"] = _strategies(conn, since=since)
        out["orders"] = _orders(conn, since=since)
        out["fills"] = _fills(conn, since=since)
    finally:
        conn.close()
    return out


def _safe_query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _account_summary(conn: sqlite3.Connection, *, since: dt.datetime) -> dict:
    rows = _safe_query(
        conn,
        "SELECT snapshot_ts, equity, cash, buying_power "
        "FROM account_snapshot WHERE snapshot_ts >= ? "
        "ORDER BY ledger_seq ASC",
        (since.isoformat(),),
    )
    if not rows:
        return {"n_snapshots": 0}
    opening = rows[0]
    latest = rows[-1]
    pnl = latest["equity"] - opening["equity"]
    pnl_pct = (pnl / opening["equity"] * 100.0) if opening["equity"] else 0.0
    return {
        "n_snapshots": len(rows),
        "opening_equity": opening["equity"],
        "opening_ts": opening["snapshot_ts"],
        "latest_equity": latest["equity"],
        "latest_ts": latest["snapshot_ts"],
        "intraday_pnl": pnl,
        "intraday_pnl_pct": pnl_pct,
        "latest_cash": latest["cash"],
        "latest_buying_power": latest["buying_power"],
    }


def _heartbeats(conn: sqlite3.Connection) -> list[dict]:
    return _safe_query(
        conn,
        "SELECT job_name, last_run_ts, last_status, last_detail, last_duration_s "
        "FROM daemon_heartbeat ORDER BY job_name",
    )


def _kill_switches(conn: sqlite3.Connection, *, since: dt.datetime) -> list[dict]:
    return _safe_query(
        conn,
        "SELECT event_ts, detector, event_kind, reason, actor "
        "FROM kill_switch_event WHERE event_ts >= ? "
        "ORDER BY ledger_seq DESC",
        (since.isoformat(),),
    )


def _strategies(conn: sqlite3.Connection, *, since: dt.datetime) -> list[dict]:
    # strategy_version has no explicit ts column in this schema; we
    # surface the full list with their lane/status — operator can scan.
    return _safe_query(
        conn,
        "SELECT strategy_id, strategy_ver, lane, status, owner, "
        "thesis_id, hypothesis_id "
        "FROM strategy_version ORDER BY strategy_id, strategy_ver",
    )


def _orders(conn: sqlite3.Connection, *, since: dt.datetime) -> list[dict]:
    return _safe_query(
        conn,
        "SELECT order_uid, symbol, qty, side, asset_class, lane, "
        "strategy_id, created_ts "
        "FROM order_master WHERE created_ts >= ? "
        "ORDER BY created_ts DESC LIMIT 50",
        (since.isoformat(),),
    )


def _fills(conn: sqlite3.Connection, *, since: dt.datetime) -> list[dict]:
    return _safe_query(
        conn,
        "SELECT event_ts, order_uid, symbol, qty, price "
        "FROM fill_event WHERE event_ts >= ? "
        "ORDER BY ledger_seq DESC LIMIT 50",
        (since.isoformat(),),
    )


def format_digest_text(d: dict) -> str:
    """Human-readable plain-text digest for terminal output."""
    lines: list[str] = []
    lines.append(f"=== trading-bot v4 digest ({d.get('window_hours', 24)}h) ===")
    lines.append(f"as of: {d.get('ts')}")
    lines.append(f"since: {d.get('since')}")
    lines.append("")
    if d.get("error"):
        lines.append(f"ERROR: {d['error']}")
        return "\n".join(lines)

    acct = d.get("account", {})
    if acct.get("n_snapshots", 0):
        lines.append("ACCOUNT")
        lines.append(f"  opening:  ${acct['opening_equity']:,.2f}  ({acct['opening_ts']})")
        lines.append(f"  latest:   ${acct['latest_equity']:,.2f}  ({acct['latest_ts']})")
        sign = "+" if acct["intraday_pnl"] >= 0 else ""
        lines.append(f"  P&L:      {sign}${acct['intraday_pnl']:,.2f}  ({sign}{acct['intraday_pnl_pct']:.2f}%)")
        lines.append(f"  cash:     ${acct['latest_cash']:,.2f}")
        lines.append(f"  buying_p: ${acct['latest_buying_power']:,.2f}")
    else:
        lines.append("ACCOUNT: no snapshots in window")

    lines.append("")
    lines.append("HEARTBEATS")
    for h in d.get("heartbeats", []):
        detail = (h.get("last_detail") or "")[:80]
        lines.append(f"  {h['job_name']:<22} {h['last_status']:<7} {detail}")

    ks = d.get("kill_switches", [])
    lines.append("")
    if ks:
        lines.append(f"KILL SWITCHES ({len(ks)} events)")
        for k in ks[:10]:
            lines.append(f"  {k['event_ts']}  {k['event_kind']:<5} {k['detector']:<24} by {k['actor']}: {(k.get('reason') or '')[:60]}")
    else:
        lines.append("KILL SWITCHES: none")

    strats = d.get("strategy_submissions", [])
    lines.append("")
    if strats:
        lines.append(f"STRATEGIES ({len(strats)})")
        for s in strats[:20]:
            lines.append(f"  {s['strategy_id']}  v{s['strategy_ver']}  {s['lane']:<8} {s['status']:<14} by {s['owner']}")
    else:
        lines.append("STRATEGIES: none registered")

    orders = d.get("orders", [])
    fills = d.get("fills", [])
    lines.append("")
    lines.append(f"ORDERS in window: {len(orders)}    FILLS in window: {len(fills)}")
    for o in orders[:5]:
        lines.append(f"  ORD {o['created_ts']}  {o['symbol']:<8} {o['side']:<6} qty={o['qty']}")
    for f in fills[:5]:
        lines.append(f"  FIL {f['event_ts']}  {f['symbol']:<8} qty={f['qty']}  price={f['price']}")

    return "\n".join(lines)


__all__ = ["build_digest", "format_digest_text"]
