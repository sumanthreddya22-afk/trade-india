"""Operator-facing insight builders for the dashboard.

Each function returns a typed dataclass that one new fragment template
renders. Built fresh per snapshot — most queries are local SQLite or
process inspection, so latency is negligible. Anything that touches
Alpaca or the filesystem is wrapped in try/except: a single broken
source must not blank the whole dashboard.

Panels powered here:
    action_required_banner   — red strip at the top of the page
    live_activity_feed       — tail of the daemon log in plain English
    email_firehose           — last 24h email volume by category
    process_registry         — running bot processes (catches orphan daemons)

Drill-down data folded into the existing Holdings table:
    stop_coverage_rows       — per-position stop status (table column)
    order_timelines          — recent order events keyed by symbol (expand row)
    why_this_trade           — decision chain keyed by symbol (expand row)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


# ---------- 1. Action-required banner ---------------------------------------

@dataclass(frozen=True)
class ActionItem:
    severity: str          # "danger" | "warn" | "info"
    headline: str          # short sentence — the operator sees this first
    detail: str            # one extra clarifying sentence
    target: str | None = None   # anchor link, e.g. "#stop_coverage"


def build_action_items(
    *, positions: list, orders: list, state_db: str = "data/state.db",
) -> list[ActionItem]:
    """Return the list of actionable issues, ordered most-urgent-first.

    Sources we check:
      - open positions without a live stop order (danger)
      - duplicate bot daemon processes (danger)
      - any background job that errored on its most recent run (warn)
      - email firehose anomaly: >5 'bad'-severity alerts in last hour (warn)
    """
    items: list[ActionItem] = []

    # 1a) Unprotected positions. We accept "stop" or "stop_limit" with any
    #     non-terminal status as protective.
    terminal = {"filled", "canceled", "expired", "rejected", "replaced",
                "done_for_day", "suspended"}

    def _canon(s: str) -> str:
        return str(s).replace("/", "").upper()

    stops_by_symbol: dict[str, int] = {}
    for o in orders or []:
        otype = (getattr(o, "order_type", "") or "").lower()
        ostatus = (getattr(o, "status", "") or "").lower()
        if otype.endswith(("stop", "stop_limit")) and ostatus not in terminal:
            stops_by_symbol[_canon(o.symbol)] = stops_by_symbol.get(_canon(o.symbol), 0) + 1

    unprotected: list[str] = []
    for p in positions or []:
        if _canon(p.symbol) not in stops_by_symbol:
            unprotected.append(p.symbol)

    if unprotected:
        items.append(ActionItem(
            severity="danger",
            headline=f"{len(unprotected)} position{'s' if len(unprotected) > 1 else ''} without a stop-loss",
            detail=(
                f"{', '.join(unprotected[:3])}"
                f"{' …' if len(unprotected) > 3 else ''} — "
                "the bot tries to attach a stop every 30 minutes; if you see this for more than an hour something is blocking it."
            ),
            target="#stop_coverage",
        ))

    # 1b) Duplicate daemon processes. The bot is supposed to run a single
    #     `python -m trading_bot.daemon`. More than one means every
    #     scheduled job (and every email) fires twice.
    try:
        out = subprocess.run(
            ["pgrep", "-f", "trading_bot.daemon"],
            capture_output=True, text=True, timeout=2,
        )
        pids = [p for p in out.stdout.strip().split("\n") if p]
        if len(pids) > 1:
            items.append(ActionItem(
                severity="danger",
                headline="Duplicate trading-bot daemon detected",
                detail=(
                    f"{len(pids)} daemons running ({', '.join(pids)}). "
                    "Every alert email and scheduled job will fire twice. "
                    "Kill the orphan and check the launch agent."
                ),
                target="#process_registry",
            ))
    except Exception:
        pass

    # 1c) Background-job failures on most recent run.
    try:
        eng = create_engine(f"sqlite:///{state_db}", future=True)
        with eng.begin() as c:
            rows = c.execute(text(
                "SELECT role_name, status, error_text "
                "FROM role_runs "
                "WHERE id IN ("
                "  SELECT MAX(id) FROM role_runs GROUP BY role_name"
                ") "
                "AND status != 'ok' "
                "AND role_name NOT IN ('health_pulse','watchdog')"
            )).all()
            failing = [r._mapping["role_name"] for r in rows]
            if failing:
                items.append(ActionItem(
                    severity="warn",
                    headline=f"{len(failing)} background job{'s' if len(failing) > 1 else ''} failed on last run",
                    detail=", ".join(failing[:5]),
                    target="#role_health",
                ))
    except Exception:
        pass

    # 1d) Email anomaly: > 5 BAD-severity alert emails in last 60 minutes.
    try:
        eng = create_engine(f"sqlite:///{state_db}", future=True)
        with eng.begin() as c:
            now = dt.datetime.now(dt.timezone.utc)
            since = (now - dt.timedelta(hours=1)).isoformat()
            n_bad = c.execute(text(
                "SELECT count(*) FROM emails_sent "
                "WHERE sent_at >= :since AND subject LIKE '[BAD]%'"
            ), {"since": since}).scalar() or 0
            if n_bad >= 5:
                items.append(ActionItem(
                    severity="warn",
                    headline=f"{n_bad} urgent alert emails in the last hour",
                    detail="That's well above the typical rate. Open the email panel to see which condition is firing.",
                    target="#email_firehose",
                ))
    except Exception:
        pass

    return items


# ---------- 2. Stop-coverage card -------------------------------------------

@dataclass(frozen=True)
class StopCoverageRow:
    symbol: str
    qty: str
    last_price: float
    stop_price: float | None
    stop_status: str         # "live", "missing", "pending"
    stop_kind: str           # "bracket leg", "standalone", "—"
    distance_pct: float | None  # how far the current price is above the stop
    note: str


def build_stop_coverage(
    *, positions: list, orders: list,
) -> list[StopCoverageRow]:
    terminal = {"filled", "canceled", "expired", "rejected", "replaced",
                "done_for_day", "suspended"}

    def _canon(s: str) -> str:
        return str(s).replace("/", "").upper()

    # Index live stops by symbol.
    stops: dict[str, list] = {}
    for o in orders or []:
        otype = (getattr(o, "order_type", "") or "").lower()
        ostatus = (getattr(o, "status", "") or "").lower()
        if otype.endswith(("stop", "stop_limit")) and ostatus not in terminal:
            stops.setdefault(_canon(o.symbol), []).append(o)

    rows: list[StopCoverageRow] = []
    for p in positions or []:
        last = float(p.last_price) if hasattr(p, "last_price") else 0.0
        sym_stops = stops.get(_canon(p.symbol), [])
        if not sym_stops:
            rows.append(StopCoverageRow(
                symbol=p.symbol, qty=str(p.qty), last_price=last,
                stop_price=None, stop_status="missing", stop_kind="—",
                distance_pct=None,
                note="No live stop right now — the next verify-stops sweep should attach one.",
            ))
            continue
        # Use the first live stop as the canonical one.
        s = sym_stops[0]
        stop_px = None
        try:
            stop_px = float(getattr(s, "stop_price", None) or 0) or None
        except Exception:
            stop_px = None
        if stop_px is None:
            # OrderRow may not expose stop_price; we'll just show "live".
            rows.append(StopCoverageRow(
                symbol=p.symbol, qty=str(p.qty), last_price=last,
                stop_price=None, stop_status="live", stop_kind="standalone",
                distance_pct=None,
                note=f"Stop is live (status: {getattr(s, 'status', '?')}) but the price isn't exposed on this order row.",
            ))
            continue
        dist = ((last - stop_px) / last * 100) if last else None
        rows.append(StopCoverageRow(
            symbol=p.symbol, qty=str(p.qty), last_price=last,
            stop_price=stop_px,
            stop_status="live" if str(getattr(s, "status", "")).lower() == "new" else "pending",
            stop_kind="standalone",
            distance_pct=round(dist, 2) if dist is not None else None,
            note=f"Stop at ${stop_px:,.2f} — about {dist:.1f}% below current." if dist is not None else "",
        ))
    return rows


# ---------- 3. Email firehose -----------------------------------------------

@dataclass(frozen=True)
class EmailBucket:
    label: str          # plain-English category
    count_24h: int
    severity: str       # "info" | "warn" | "danger"


@dataclass(frozen=True)
class EmailFirehose:
    total_24h: int
    by_bucket: list[EmailBucket]
    busiest_hour_count: int
    busiest_hour_label: str | None       # e.g. "9–10am ET"
    note: str                             # short summary line


def _bucket_email(subject: str) -> tuple[str, str]:
    """Return (label, severity) for an emails_sent row."""
    s = (subject or "").lower()
    if s.startswith("[bad]"):
        return ("Urgent alerts (BAD)", "danger")
    if s.startswith("[warn]"):
        return ("Warnings (WARN)", "warn")
    if "eod report" in s or "daily digest" in s:
        return ("Daily reports", "info")
    if "midday" in s:
        return ("Midday snapshots", "info")
    if "intel scan" in s:
        return ("Intel scan summaries", "info")
    if s.startswith("[info]") or "open positions" in s:
        return ("Position updates", "info")
    if s.startswith("status ·") or "trading bot — status" in s:
        return ("Status checks", "info")
    return ("Other", "info")


def build_email_firehose(
    *, state_db: str = "data/state.db",
) -> EmailFirehose | None:
    try:
        eng = create_engine(f"sqlite:///{state_db}", future=True)
        with eng.begin() as c:
            now = dt.datetime.now(dt.timezone.utc)
            since = (now - dt.timedelta(hours=24)).isoformat()
            rows = c.execute(text(
                "SELECT sent_at, subject FROM emails_sent "
                "WHERE sent_at >= :since ORDER BY sent_at"
            ), {"since": since}).all()
    except Exception:
        return None

    if not rows:
        return EmailFirehose(
            total_24h=0, by_bucket=[], busiest_hour_count=0,
            busiest_hour_label=None,
            note="No emails sent in the last 24 hours.",
        )

    bucket_counts: dict[str, tuple[int, str]] = {}
    hour_counts: dict[int, int] = {}
    for r in rows:
        label, sev = _bucket_email(str(r._mapping["subject"]))
        prev = bucket_counts.get(label, (0, sev))
        bucket_counts[label] = (prev[0] + 1, sev)
        ts = r._mapping["sent_at"]
        if isinstance(ts, str):
            try:
                ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        et = ts.astimezone(dt.timezone(dt.timedelta(hours=-4)))  # ET ≈ UTC-4
        hour_counts[et.hour] = hour_counts.get(et.hour, 0) + 1

    busiest_hour, busiest_count = max(hour_counts.items(), key=lambda kv: kv[1])
    next_hr = (busiest_hour + 1) % 24
    label = f"{busiest_hour % 12 or 12}–{next_hr % 12 or 12}{'pm' if busiest_hour >= 12 else 'am'} ET"

    by_bucket = [
        EmailBucket(label=k, count_24h=v[0], severity=v[1])
        for k, v in sorted(bucket_counts.items(), key=lambda kv: -kv[1][0])
    ]
    total = sum(b.count_24h for b in by_bucket)
    if total > 30:
        note = f"That's {total} emails in 24 hours — way above a typical day. Top category: {by_bucket[0].label}."
    elif total > 15:
        note = f"{total} emails today — slightly elevated. Top category: {by_bucket[0].label}."
    else:
        note = f"{total} emails over the last 24 hours — normal volume."
    return EmailFirehose(
        total_24h=total, by_bucket=by_bucket,
        busiest_hour_count=busiest_count, busiest_hour_label=label,
        note=note,
    )


# ---------- 4. Process registry ---------------------------------------------

@dataclass(frozen=True)
class ProcessRow:
    pid: int
    parent_pid: int
    label: str           # "trading bot daemon", "supervisor", etc.
    started: str         # human-readable start time
    expected: bool       # is this one of the canonical processes?
    raw_command: str


def build_process_registry() -> list[ProcessRow]:
    try:
        out = subprocess.run(
            ["ps", "-axwwo", "pid=,ppid=,lstart=,command="],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return []
    rows: list[ProcessRow] = []
    seen_labels: set[str] = set()
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if "trading_bot" not in line and "/bot " not in line and "bot daemon" not in line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        rest = parts[2]
        # `ps lstart` produces "Wed Apr 30 06:01:57 2026 <command...>"
        m = re.match(r"^(\w{3} \w{3} ?\s*\d+ \d+:\d+:\d+ \d+)\s+(.*)$", rest)
        if not m:
            continue
        started_raw, cmd = m.group(1), m.group(2)
        if "ps -axwwo" in cmd or "pgrep" in cmd:
            continue
        # Skip OS-level launcher wrappers (Gatekeeper disclaimer, etc.) that
        # exec the real python process — they share the child's command line
        # and would falsely count as duplicates.
        if cmd.startswith("/Applications/") and "disclaimer" in cmd:
            continue
        # Friendly label
        if "trading_bot.daemon" in cmd or cmd.endswith("bot daemon") or " bot daemon" in cmd:
            label = "Daemon (scheduler + verify-stops + alerts)"
        elif "trading_bot.supervisor" in cmd:
            label = "Supervisor (account watchdog)"
        elif "trading_bot.lab" in cmd:
            label = "Lab (nightly tuning)"
        elif "dashboard" in cmd:
            label = "Dashboard (this web UI)"
        else:
            label = "Trading-bot related"
        # Is this an expected single-instance process?
        expected = label not in seen_labels
        seen_labels.add(label)
        rows.append(ProcessRow(
            pid=pid, parent_pid=ppid, label=label,
            started=started_raw, expected=expected,
            raw_command=cmd[:140],
        ))
    return rows


# ---------- 5. Per-symbol order timeline ------------------------------------

@dataclass(frozen=True)
class TimelineEvent:
    when_iso: str
    when_label: str
    kind: str            # "buy submitted" | "buy filled" | "stop active" | etc.
    detail: str          # one-line plain English
    severity: str        # "info" | "good" | "warn" | "danger"


@dataclass(frozen=True)
class SymbolTimeline:
    symbol: str
    events: list[TimelineEvent] = field(default_factory=list)


def build_order_timelines(*, settings) -> list[SymbolTimeline]:
    """Walk Alpaca orders + bracket legs from the last 36 hours and emit a
    chronological timeline per symbol. Useful when something looked weird
    today (e.g. ARM today) and you want to see the order's life story.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=36)
        orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=200, after=since, nested=True,
        ))
    except Exception:
        return []

    by_sym: dict[str, list[TimelineEvent]] = {}

    def _add(symbol: str, when: dt.datetime | None, kind: str,
             detail: str, severity: str = "info") -> None:
        if when is None:
            return
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        et = when.astimezone(dt.timezone(dt.timedelta(hours=-4)))
        label = et.strftime("%b %d · %-I:%M %p ET")
        by_sym.setdefault(symbol, []).append(TimelineEvent(
            when_iso=when.isoformat(), when_label=label,
            kind=kind, detail=detail, severity=severity,
        ))

    def _walk(o, parent_kind: str = "") -> None:
        sym = str(o.symbol)
        side = str(o.side).split(".")[-1].lower()
        otype = str(o.order_type).split(".")[-1].lower()
        oclass = str(getattr(o, "order_class", "")).split(".")[-1].lower()
        status = str(o.status).split(".")[-1].lower()

        # Submission
        if o.submitted_at:
            verb = side
            if otype.startswith("stop"):
                verb = f"protective {side} stop"
            elif "limit" in otype:
                verb = f"{side} limit"
            else:
                verb = f"{side} {otype}"
            _add(
                sym, o.submitted_at,
                f"{verb} submitted",
                f"Order submitted to broker — qty {o.qty}, "
                f"status: {status}{' (' + oclass + ')' if oclass and oclass != 'simple' else ''}.",
                "info",
            )
        # Fill
        if getattr(o, "filled_at", None):
            _add(sym, o.filled_at, f"{side} filled",
                 f"Filled {o.filled_qty or o.qty} at ${getattr(o, 'filled_avg_price', None)}.",
                 "good")
        # Cancel
        if getattr(o, "canceled_at", None):
            _add(sym, o.canceled_at, "canceled",
                 "Order canceled (often EOD cleanup of dangling stops).",
                 "warn" if otype.startswith("stop") else "info")
        # Expire
        if getattr(o, "expired_at", None):
            _add(sym, o.expired_at, "expired",
                 "Order expired (e.g. day-only TP leg at market close).",
                 "info")
        # Walk children
        for leg in (getattr(o, "legs", None) or []):
            _walk(leg, parent_kind=otype)

    for o in orders:
        try:
            _walk(o)
        except Exception:
            continue

    timelines = []
    for sym, events in by_sym.items():
        events.sort(key=lambda e: e.when_iso)
        timelines.append(SymbolTimeline(symbol=sym, events=events))
    timelines.sort(key=lambda t: t.symbol)
    return timelines


# ---------- 6. Live activity feed -------------------------------------------

@dataclass(frozen=True)
class ActivityLine:
    when_label: str       # "1:45 PM ET"
    level: str            # "info" | "warn" | "error"
    event: str            # short event tag
    detail: str           # one-line readable summary
    # Phase 2 — named operators that staffed this run, when the event maps
    # to a known role with personas. Each item is {name, debate_role,
    # title, pipeline}. Empty list when the event is non-LLM (data fetch,
    # log rotation).
    actors: list[dict] = field(default_factory=list)


_ACTIVITY_VERBS = {
    "verify_stops_start": "Checking stop-loss coverage",
    "verify_stops_finish": "Stop-loss check complete",
    "alert_drain_start": "Sending queued alerts",
    "alert_drain_finish": "Alerts sent",
    "stock_scanner_start": "Scanning stocks",
    "stock_scanner_finish": "Stock scan complete",
    "crypto_scanner_start": "Scanning crypto",
    "crypto_scanner_finish": "Crypto scan complete",
    "midday_rerank_start": "Re-ranking watchlist",
    "midday_rerank_finish": "Watchlist updated",
    "midday_snapshot_start": "Building midday report",
    "midday_snapshot_finish": "Midday report sent",
    "midday_snapshot_failed": "Midday report failed",
    "daily_digest_start": "Building end-of-day report",
    "daily_digest_finish": "End-of-day report sent",
    "portfolio_watch_start": "Checking portfolio for events",
    "portfolio_watch_finish": "Portfolio check complete",
    "wheel_scan_start": "Scanning options wheel",
    "wheel_scan_finish": "Options wheel scan complete",
    "wheel_manage_start": "Managing options positions",
    "wheel_manage_finish": "Options management complete",
    "iv_capture_start": "Capturing options volatility",
    "iv_capture_finish": "Options volatility captured",
    "premarket_rank_start": "Building morning watchlist",
    "premarket_rank_finish": "Morning watchlist ready",
    "scheduler_started": "Scheduler started",
    "vip_scan_start": "Checking VIP feeds",
    "vip_scan_finish": "VIP feed check complete",
    "news_warm_start": "Refreshing news sentiment",
    "news_warm_finish": "News sentiment refreshed",
    "reconciler_close": "Reconciling end-of-day positions",
    "massive_refresh": "Refreshing market data",
    "log_rotation": "Rotating log files",
    "email_sent": "Email sent",
    "daemon_boot": "Daemon started",
    "daemon_stopping": "Daemon stopping",
    "daemon_stopped": "Daemon stopped",
}


def _iter_activity_rows(runs_dir: Path, role: str) -> Any:
    """Yield event dicts newest-first from runs/<UTC date>/<role>/*.json.

    StructuredLogger writes one JSON file per event (HH-MM-SS[.usec].json).
    Lex sort = chronological, so reverse-sort gives newest-first. Walks
    today's UTC date dir, then yesterday's so the feed survives midnight.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    for delta in (0, 1):
        d = (today - dt.timedelta(days=delta)).isoformat()
        role_dir = runs_dir / d / role
        if not role_dir.is_dir():
            continue
        try:
            files = sorted(role_dir.iterdir(), reverse=True)
        except OSError:
            continue
        for fp in files:
            try:
                yield json.loads(fp.read_text())
            except Exception:
                continue


def _event_to_role_name(event: str) -> str | None:
    """Map a daemon log event tag to a role_name (the APScheduler key).

    Activity events look like ``crypto_scan_finish``, ``crypto_scanner_finish``,
    ``portfolio_watch_start``, etc. The role name is the prefix before
    the trailing ``_start`` / ``_finish`` / ``_failed`` / ``_skipped``.
    Some legacy events use ``<thing>_scanner_finish`` (with the extra
    "_scanner" suffix); strip that too so the lookup matches role-runner
    keys.
    """
    if not event:
        return None
    for suffix in ("_finish", "_start", "_failed", "_skipped"):
        if event.endswith(suffix):
            base = event[: -len(suffix)]
            # ``stock_scanner`` log → ``intel_scan`` role; ``crypto_scanner``
            # → ``crypto_scan``. Map the legacy alias.
            alias = {
                "stock_scanner": "intel_scan",
                "crypto_scanner": "crypto_scan",
            }.get(base)
            return alias or base
    return None


def build_activity_feed(
    *, runs_dir: str | Path = "runs", role: str = "daemon", limit: int = 80,
) -> list[ActivityLine]:
    # Late import so a circular dependency in role_persona_map cannot
    # break the activity feed builder.
    try:
        from trading_bot.shared.role_persona_map import operators_payload
    except Exception:
        def operators_payload(_role_name: str) -> list[dict]:  # type: ignore[misc]
            return []

    out: list[ActivityLine] = []
    for row in _iter_activity_rows(Path(runs_dir), role):
        if len(out) >= limit:
            break
        event = row.get("event")
        ts = row.get("ts") or ""
        level = (row.get("level") or "info").lower()
        if not event:
            continue
        # Suppress heartbeat noise: alert_drain runs every ~30s, and the
        # `_start` half of every job pair is redundant with its `_finish`.
        # We keep `_finish` events at info so the operator sees what just
        # completed — that's the whole point of this panel.
        if event in ("alert_drain_start", "alert_drain_finish"):
            continue
        if event.endswith("_start") and level == "info":
            continue
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            et = t.astimezone(dt.timezone(dt.timedelta(hours=-4)))
            when = et.strftime("%-I:%M:%S %p ET")
        except Exception:
            when = ts[:19]
        verb = _ACTIVITY_VERBS.get(event, event.replace("_", " "))
        detail = row.get("error_message") or row.get("message") or ""
        if event.endswith("_failed"):
            detail = f"Failed: {row.get('error_message', '?')}"
        elif event == "email_sent":
            # Show subject so the operator sees what just went out, not just "email sent".
            subject = row.get("subject") or ""
            kind = row.get("kind") or ""
            detail = f"{subject}" + (f" ({kind})" if kind else "")

        # Phase 2 — attach actor names when the event maps to a role
        # whose operators are known. Skip on _start so the same names
        # don't appear twice in the feed (start + finish).
        actors: list[dict] = []
        if event.endswith(("_finish", "_failed")):
            role_name = _event_to_role_name(event)
            if role_name:
                try:
                    actors = operators_payload(role_name)
                except Exception:
                    actors = []
        out.append(ActivityLine(
            when_label=when,
            level="error" if event.endswith("_failed") or level == "error"
                  else ("warn" if level == "warn" else "info"),
            event=verb, detail=detail[:160],
            actors=actors,
        ))
    return out


# ---------- 7. Why-this-trade -----------------------------------------------

@dataclass(frozen=True)
class TradeRationale:
    symbol: str
    entry_at: str | None
    confidence: float | None
    primary_reason: str
    notes: list[str]


def build_why_this_trade(
    *, positions: list, state_db: str = "data/state.db",
) -> list[TradeRationale]:
    if not positions:
        return []
    out: list[TradeRationale] = []
    try:
        eng = create_engine(f"sqlite:///{state_db}", future=True)
    except Exception:
        return []
    for p in positions:
        sym = p.symbol
        with eng.begin() as c:
            row = c.execute(text(
                "SELECT timestamp_utc, action, reason, confidence, "
                "       expected_edge_bps, audit_json "
                "FROM decisions "
                "WHERE symbol = :sym AND action = 'placed_order' "
                "ORDER BY timestamp_utc DESC LIMIT 1"
            ), {"sym": sym}).first()
        if not row:
            out.append(TradeRationale(
                symbol=sym, entry_at=None, confidence=None,
                primary_reason="No decision record found for this position. "
                               "It may pre-date the decision-log feature.",
                notes=[],
            ))
            continue
        m = row._mapping
        notes: list[str] = []
        try:
            audit = json.loads(m["audit_json"] or "{}")
            for k in ("regime", "strategy", "intel_signals", "signal"):
                if k in audit and audit[k]:
                    notes.append(f"{k}: {audit[k]}")
        except Exception:
            pass
        edge = m["expected_edge_bps"]
        if edge:
            notes.append(f"Expected edge: {edge:.0f} bps")
        out.append(TradeRationale(
            symbol=sym,
            entry_at=str(m["timestamp_utc"]),
            confidence=m["confidence"],
            primary_reason=m["reason"] or "—",
            notes=notes,
        ))
    return out


# ---------- top-level snapshot -----------------------------------------------

@dataclass(frozen=True)
class InsightsSnapshot:
    action_items: list[ActionItem]
    stop_coverage: list[StopCoverageRow]
    email_firehose: EmailFirehose | None
    process_registry: list[ProcessRow]
    order_timelines: dict[str, list[TimelineEvent]]   # keyed by symbol for fast template lookup
    activity_feed: list[ActivityLine]
    why_trades: dict[str, TradeRationale]              # keyed by symbol


def build_insights(
    *, settings: Any, positions: list, orders: list,
    state_db: str | None = None,
) -> InsightsSnapshot:
    state_db = state_db or os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
    try:
        action_items = build_action_items(
            positions=positions, orders=orders, state_db=state_db,
        )
    except Exception:
        action_items = []
    try:
        stop_coverage = build_stop_coverage(positions=positions, orders=orders)
    except Exception:
        stop_coverage = []
    try:
        firehose = build_email_firehose(state_db=state_db)
    except Exception:
        firehose = None
    try:
        registry = build_process_registry()
    except Exception:
        registry = []
    try:
        timelines = {t.symbol: t.events for t in build_order_timelines(settings=settings)}
    except Exception:
        timelines = {}
    try:
        feed = build_activity_feed()
    except Exception:
        feed = []
    try:
        why = {t.symbol: t for t in build_why_this_trade(positions=positions, state_db=state_db)}
    except Exception:
        why = {}
    return InsightsSnapshot(
        action_items=action_items,
        stop_coverage=stop_coverage,
        email_firehose=firehose,
        process_registry=registry,
        order_timelines=timelines,
        activity_feed=feed,
        why_trades=why,
    )
