"""tools/inspect_state.py — one-shot 'is the bot healthy?' snapshot.

Single command that answers the questions you'd otherwise piece together
across 5 sqlite queries, the daemon log, and the heartbeat file:

  * Is the daemon alive? Heartbeat age?
  * Is trading gated off? Why? Set by whom?
  * Any role currently stalled?
  * What did the last scanner runs decide?
  * Today's order audit — anything placed?
  * Today's debate count — any committee fires?
  * Mailbox queue depth (anything stuck pending?)

Usage:
    .venv/bin/python tools/inspect_state.py
    .venv/bin/python tools/inspect_state.py --json   # machine-readable
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _heartbeat_status(path: Path) -> dict:
    if not path.exists():
        return {"present": False}
    try:
        data = json.loads(path.read_text())
        ts = dt.datetime.fromisoformat(str(data.get("ts")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        age = (_utc_now() - ts).total_seconds()
        return {
            "present": True,
            "ts": data.get("ts"),
            "pid": data.get("pid"),
            "version": data.get("version"),
            "last_action": data.get("last_action"),
            "age_seconds": int(age),
            "fresh": age < 120,
        }
    except Exception as e:
        return {"present": True, "error": str(e)}


def _fallback_state(state_db: Path) -> dict:
    try:
        from sqlalchemy.orm import Session
        from trading_bot.state_db import get_engine
        from trading_bot.state_fallback import current_flag
        eng = get_engine(state_db)
        with Session(eng) as s:
            flag = current_flag(s)
            if flag is None:
                return {"present": False}
            return {
                "present": True,
                "active": bool(flag.fallback_active),
                "set_at": flag.set_at.isoformat() if hasattr(flag.set_at, "isoformat") else str(flag.set_at),
                "set_by": flag.set_by,
                "reason": flag.reason,
            }
    except Exception as e:
        return {"present": False, "error": str(e)}


def _stalled_roles(state_db: Path) -> list[dict]:
    try:
        from trading_bot.supervisor import _build_role_sla_map, _find_stalled_roles
        return _find_stalled_roles(state_db, sla_map=_build_role_sla_map())
    except Exception as e:
        return [{"error": str(e)}]


def _last_scan_summary() -> dict:
    out: dict[str, Any] = {}
    for label, path in (
        ("equity_scan", Path("data/last_scan.json")),
        ("wheel_scan", Path("data/wheel_scan_last.json")),
    ):
        if not path.exists():
            out[label] = None
            continue
        try:
            payload = json.loads(path.read_text())
            if label == "equity_scan":
                out[label] = {
                    "command": payload.get("command"),
                    "regime": payload.get("regime"),
                    "universe_size": payload.get("universe_size"),
                    "timestamp": payload.get("timestamp"),
                    "decisions": len(payload.get("decisions", [])),
                }
            else:
                out[label] = {
                    "started_at": payload.get("started_at"),
                    "finished_at": payload.get("finished_at"),
                    "universe_size": payload.get("universe_size"),
                    "orders_placed": payload.get("orders_placed"),
                    "preflight_skipped": payload.get("preflight_skipped"),
                    "no_contract_picked": payload.get("no_contract_picked"),
                    "risk_alloc_rejected": payload.get("risk_alloc_rejected"),
                }
        except Exception as e:
            out[label] = {"error": str(e)}
    return out


def _orders_today() -> dict:
    """Count order_submitted audit events from runs/<today>/alpaca/."""
    today = _utc_now().date().isoformat()
    base = Path("runs") / today / "alpaca"
    if not base.exists():
        return {"count": 0, "by_source": {}}
    files = list(base.glob("*.json"))
    by_source: dict[str, int] = {}
    for f in files:
        try:
            payload = json.loads(f.read_text())
            if payload.get("event") == "order_submitted":
                src = str(payload.get("source", "unknown"))
                by_source[src] = by_source.get(src, 0) + 1
        except Exception:
            continue
    return {"count": sum(by_source.values()), "by_source": by_source}


def _debates_today(state_db: Path) -> dict:
    try:
        from sqlalchemy.orm import Session
        from sqlalchemy import func
        from trading_bot.state_db import get_engine, UnblockDebateRun
        eng = get_engine(state_db)
        today_utc = dt.datetime.combine(
            _utc_now().date(), dt.time.min, tzinfo=dt.timezone.utc,
        )
        with Session(eng) as s:
            rows = s.query(UnblockDebateRun).filter(
                UnblockDebateRun.run_at >= today_utc,
            ).all()
            verdicts: dict[str, int] = {}
            for r in rows:
                verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
            return {
                "count": len(rows),
                "verdicts": verdicts,
                "symbols": sorted({r.symbol for r in rows}),
            }
    except Exception as e:
        return {"count": 0, "error": str(e)}


def _mailbox_depth() -> dict:
    try:
        from trading_bot.llm_mailbox import MailboxQueue
        return MailboxQueue(base="data/llm_queue").stats()
    except Exception as e:
        return {"error": str(e)}


def _ascii_section(title: str, body: str) -> str:
    return f"\n=== {title} ===\n{body}"


def _format_human(snap: dict) -> str:
    lines: list[str] = []
    hb = snap["heartbeat"]
    if hb.get("present"):
        marker = "OK" if hb.get("fresh") else "STALE"
        lines.append(_ascii_section(
            f"DAEMON HEARTBEAT [{marker}]",
            (
                f"  pid:         {hb.get('pid')}\n"
                f"  ts:          {hb.get('ts')} (age {hb.get('age_seconds', '?')}s)\n"
                f"  last_action: {hb.get('last_action')}\n"
                f"  version:     {hb.get('version')}"
            ),
        ))
    else:
        lines.append(_ascii_section(
            "DAEMON HEARTBEAT [MISSING]",
            "  data/heartbeat.json not found — daemon may not be running",
        ))

    f = snap["fallback"]
    if not f.get("present"):
        lines.append(_ascii_section("TRADING GATE [UNKNOWN]", "  no fallback_flags row"))
    elif f.get("active"):
        lines.append(_ascii_section(
            "TRADING GATE [HALTED]",
            (
                f"  set_at: {f.get('set_at')}\n"
                f"  set_by: {f.get('set_by')}\n"
                f"  reason: {f.get('reason')}"
            ),
        ))
    else:
        lines.append(_ascii_section(
            "TRADING GATE [OPEN]",
            (
                f"  cleared at: {f.get('set_at')}\n"
                f"  cleared by: {f.get('set_by')}\n"
                f"  reason:     {f.get('reason')}"
            ),
        ))

    stalled = snap["stalled_roles"]
    if not stalled:
        lines.append(_ascii_section("STALLED ROLES [NONE]", "  no roles overdue"))
    else:
        body = "\n".join(
            f"  {s.get('role_name')} — age {s.get('age_seconds')}s "
            f"(threshold {s.get('threshold_seconds')}s)"
            for s in stalled
        )
        lines.append(_ascii_section(f"STALLED ROLES [{len(stalled)}]", body))

    last = snap["last_scans"]
    eq = last.get("equity_scan") or {}
    wh = last.get("wheel_scan") or {}
    lines.append(_ascii_section(
        "LAST SCANS",
        (
            f"  equity:  {eq.get('command') or '(none)'} @ {eq.get('timestamp', '?')}  "
            f"  · {eq.get('decisions', 0)} decisions, regime={eq.get('regime')}\n"
            f"  wheel:   started {wh.get('started_at', '(none)')}\n"
            f"           finished {wh.get('finished_at', '(none)')} | "
            f"universe={wh.get('universe_size', 0)} placed={wh.get('orders_placed', 0)} "
            f"preflight_skip={wh.get('preflight_skipped', 0)} "
            f"no_contract={wh.get('no_contract_picked', 0)} "
            f"risk_reject={wh.get('risk_alloc_rejected', 0)}"
        ),
    ))

    o = snap["orders_today"]
    lines.append(_ascii_section(
        f"ORDERS SUBMITTED TODAY [{o['count']}]",
        ("  " + ", ".join(f"{k}={v}" for k, v in o.get("by_source", {}).items()))
        if o.get("by_source") else "  none",
    ))

    d = snap["debates_today"]
    lines.append(_ascii_section(
        f"UNBLOCK DEBATES TODAY [{d.get('count', 0)}]",
        (
            ("  symbols: " + ", ".join(d.get("symbols", [])) + "\n  verdicts: "
             + ", ".join(f"{k}={v}" for k, v in d.get("verdicts", {}).items()))
            if d.get("count") else "  none"
        ),
    ))

    m = snap["mailbox"]
    lines.append(_ascii_section(
        "LLM MAILBOX QUEUE",
        f"  pending={m.get('pending', '?')} done={m.get('done', '?')} "
        f"processed={m.get('processed', '?')} failed={m.get('failed', '?')}",
    ))

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--state-db", default=os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"),
    )
    p.add_argument(
        "--heartbeat", default=os.environ.get("TRADING_BOT_HEARTBEAT", "data/heartbeat.json"),
    )
    p.add_argument("--json", action="store_true", help="emit JSON instead of human text")
    args = p.parse_args()

    snap = {
        "as_of": _utc_now().isoformat(),
        "heartbeat": _heartbeat_status(Path(args.heartbeat)),
        "fallback": _fallback_state(Path(args.state_db)),
        "stalled_roles": _stalled_roles(Path(args.state_db)),
        "last_scans": _last_scan_summary(),
        "orders_today": _orders_today(),
        "debates_today": _debates_today(Path(args.state_db)),
        "mailbox": _mailbox_depth(),
    }

    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(_format_human(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
