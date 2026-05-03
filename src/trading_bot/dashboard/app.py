"""FastAPI app for the local trading dashboard.

Per-section HTMX refresh: each card auto-refreshes its own fragment so
the user keeps scroll position, focus, and chart instances. A single
DashboardSnapshot is built every ~25s by a background thread; requests
serve from cache. Concurrent cache misses are coalesced behind a lock.

Routes
------
- GET /                            full page
- GET /architecture                static page
- GET /fragment/{name}             one section (HTMX target)
- GET /api/snapshot                JSON
- GET /api/equity-curve?range=...  JSON; range ∈ 1w|1m|3m|ytd|all
- GET /api/market-session          JSON; pre|rth|after|closed
- GET /refresh                     forced re-render (full page)
"""
from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trading_bot.shared.config import Settings, load_config
from trading_bot.dashboard.data import DashboardSnapshot, build_snapshot
from trading_bot.dashboard.insights import InsightsSnapshot, build_insights
from trading_bot.event_bus import bus as _bus_mod
from trading_bot.event_bus.subscriber import Broadcaster, Event, get_max_event_id

CONFIG_PATH = Path("strategy/config.yaml")
WATCHLIST_PATH = Path("strategy/watchlist.yaml")
OPPORTUNITIES_PATH = Path("strategy/opportunities.md")
CLOSED_DB_PATH = Path("data/closed_trades.db")

CACHE_TTL_SECONDS = 25
BG_REFRESH_INTERVAL = 25

# Path to the shared SQLite that holds the events table. Producers across
# all four launchd processes write here; the SSE broadcaster tails it.
STATE_DB_PATH = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")

# Allowed Host values for the SSE endpoint. The dashboard binds 127.0.0.1
# but a malicious browser tab on a public site could try DNS rebinding;
# rejecting unknown Host headers is a one-line defense. The list is a
# prefix match so port-only differences (8765, 8000, etc) still pass.
_ALLOWED_HOSTS = ("127.0.0.1", "localhost")

# Server-side reconnect hint sent in the SSE preamble. Browser will wait
# this long (plus its own jitter) before reconnecting after a transient
# network blip. Add jitter per-connection to avoid reconnect storms.
_SSE_RETRY_BASE_MS = 5000
_SSE_RETRY_JITTER_MS = 2000

# Heartbeat comment cadence. EventSource will treat the connection as
# alive as long as it sees bytes within ~30s; a comment line every 15s
# is conservative. Bytes are tiny (`: hb\n\n`).
_SSE_HEARTBEAT_S = 15.0

# Whitelist of fragment names → partial template files. Each fragment is
# its own HTMX refresh target.
FRAGMENTS: dict[str, str] = {
    # Top-of-page triage
    "action_required": "_action_required.html",
    "header": "_header.html",
    # "Right Now" section
    "strategy_mode": "_strategy_mode.html",
    "regime": "_regime.html",   # now folds in the macro feed
    "kpi": "_kpi.html",
    "risk": "_risk.html",
    "exposure": "_exposure.html",  # holdings table with stop column + drill-down rows
    "equity": "_equity.html",
    "orders": "_orders.html",
    "activity_feed": "_activity_feed.html",   # NEW: live log tail
    # Recent activity
    "decision_activity": "_decision_activity.html",
    "lessons": "_lessons.html",
    "last_scan": "_last_scan.html",
    "opportunities": "_opportunities.html",
    "stats": "_stats.html",
    # Strategy lab
    "lab_evolution": "_lab_evolution.html",
    "calibrator": "_calibrator.html",
    "threshold_overrides": "_threshold_overrides.html",
    "intel_pool": "_intel_pool.html",
    "proposals": "_proposals.html",
    "llm_spend": "_llm_spend.html",
    "wheel": "_wheel.html",
    # System health
    "role_health": "_role_health.html",
    "scheduled": "_scheduled.html",
    "freshness": "_freshness.html",
    "email_firehose": "_email_firehose.html",  # NEW
    "process_registry": "_process_registry.html",  # NEW
    "sidebar_status": "_sidebar_status.html",
}


@dataclass
class _CombinedSnapshot:
    """Holds the data + insights bundles together. Built once per cache
    refresh so every fragment sees a consistent view (one Alpaca read,
    one log tail, one email count)."""
    data: DashboardSnapshot
    insights: InsightsSnapshot | None


class _SnapshotCache:
    """Thread-safe snapshot cache with single-flight semantics."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._stamp: float = 0.0
        self._snap: _CombinedSnapshot | None = None
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()

    def _is_fresh(self, now: float) -> bool:
        return self._snap is not None and (now - self._stamp) <= self._ttl

    def get(self) -> _CombinedSnapshot:
        now = time.time()
        with self._lock:
            if self._is_fresh(now):
                return self._snap  # type: ignore[return-value]

        # Single-flight: only one thread builds; others wait then re-read.
        with self._build_lock:
            with self._lock:
                if self._is_fresh(time.time()):
                    return self._snap  # type: ignore[return-value]
            snap = self._build()
            with self._lock:
                self._snap = snap
                self._stamp = time.time()
            return snap

    def force_refresh(self) -> _CombinedSnapshot:
        with self._build_lock:
            snap = self._build()
            with self._lock:
                self._snap = snap
                self._stamp = time.time()
            return snap

    def invalidate(self) -> None:
        with self._lock:
            self._snap = None
            self._stamp = 0.0

    def _build(self) -> _CombinedSnapshot:
        data = build_snapshot(
            settings=Settings(),
            config=load_config(CONFIG_PATH),
            opportunities_path=OPPORTUNITIES_PATH,
            watchlist_path=WATCHLIST_PATH,
            closed_db_path=CLOSED_DB_PATH,
        )
        try:
            insights = build_insights(
                settings=Settings(),
                positions=data.positions,
                orders=data.orders,
            )
        except Exception:
            insights = None
        return _CombinedSnapshot(data=data, insights=insights)


def _start_background_refresher(cache: _SnapshotCache, interval: float) -> threading.Event:
    """Spawn a daemon thread that keeps the cache warm. Returns a stop event."""
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                cache.force_refresh()
            except Exception:
                # Snapshot builder is supposed to never raise, but be defensive.
                pass

    t = threading.Thread(target=_loop, name="dashboard-cache-refresher", daemon=True)
    t.start()
    return stop


# ---------- market session ------------------------------------------------

_NY = ZoneInfo("America/New_York")


def _market_session(now_utc: datetime | None = None) -> tuple[str, str]:
    """Return (code, label). code ∈ pre|rth|after|closed. Treats holidays as
    plain weekday hours — we don't have a holiday calendar handy and the chip
    is informational, not gating logic."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(_NY)
    wd = now.weekday()  # Mon=0
    if wd >= 5:
        return "closed", "Market closed (weekend)"
    t = now.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        return "pre", "Pre-market"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "rth", "Regular hours"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "after", "After-hours"
    return "closed", "Market closed"


# ---------- equity range filtering ---------------------------------------

_RANGE_DAYS = {"1w": 7, "1m": 30, "3m": 90, "ytd": -1, "all": -2}


def _filter_equity_range(points: list[Any], range_key: str) -> list[Any]:
    """Slice equity_curve to the requested range. `points` are EquityPoint."""
    if not points:
        return points
    if range_key == "all":
        return points
    now = datetime.now(timezone.utc)
    if range_key == "ytd":
        cutoff = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        return [p for p in points if p.ts >= cutoff]
    days = _RANGE_DAYS.get(range_key, 30)
    if days <= 0:
        return points
    cutoff_ts = now.timestamp() - days * 86400
    return [p for p in points if p.ts.timestamp() >= cutoff_ts]


# ---------- app factory ---------------------------------------------------


def _last_role_runs(db_path: str, role_name: str, *, limit: int = 10) -> list[Any]:
    """Most-recent-first role_runs for one role. Returns SimpleNamespace
    rows so the template can use dot-access."""
    import sqlite3 as _sql
    from types import SimpleNamespace
    out: list[SimpleNamespace] = []
    try:
        with _sql.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0) as conn:
            rows = conn.execute(
                "SELECT role_name, started_at, finished_at, status, latency_ms, error_text "
                "FROM role_runs WHERE role_name = ? ORDER BY started_at DESC LIMIT ?",
                (role_name, limit),
            ).fetchall()
        for r in rows:
            ts = r[1]
            try:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                pass
            out.append(SimpleNamespace(
                role_name=r[0], started_at=ts, finished_at=r[2],
                status=r[3], latency_ms=r[4], error_text=r[5] or "",
            ))
    except Exception:
        pass
    return out


def _recent_events_for(db_path: str, types: tuple[str, ...], *, limit: int = 20) -> list[Any]:
    """Most-recent-first events filtered to ``types``."""
    import json as _json
    import sqlite3 as _sql
    from types import SimpleNamespace
    out: list[SimpleNamespace] = []
    if not types:
        return out
    placeholders = ",".join(["?"] * len(types))
    try:
        with _sql.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0) as conn:
            rows = conn.execute(
                f"SELECT type, payload, source, created_at FROM events "
                f"WHERE type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*types, limit),
            ).fetchall()
        for r in rows:
            ts = r[3]
            try:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                pass
            try:
                payload = _json.loads(r[1] or "{}")
                payload_str = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:6])
            except Exception:
                payload_str = ""
            out.append(SimpleNamespace(
                type=r[0], payload_str=payload_str, source=r[2], created_at=ts,
            ))
    except Exception:
        pass
    return out


async def _maybe_start_market_data_stream(app, broadcaster, cache) -> None:
    """Phase 8: live market-data ticks for held + top-N watchlist symbols.

    Gated behind ``TRADING_BOT_DASHBOARD_LIVE_PRICES``. When on, opens
    Alpaca's ``StockDataStream`` from the dashboard process (so ticks
    never round-trip through SQLite), then schedules a background task
    that listens for ``position.changed`` / ``opportunities.updated`` /
    ``intel.updated`` events and asks the runner to recompute its
    symbol set (debounced, 5s).
    """
    try:
        from trading_bot.shared.config import Settings
        from trading_bot.streams.market_data_stream import maybe_start as _md_start
    except Exception:
        return
    top_n = int(os.environ.get("TRADING_BOT_DASHBOARD_PRICE_TOP_N", "10"))

    def _provider() -> list[str]:
        # Held positions ∪ top-N watchlist (stocks only; crypto handled
        # via a separate stream in a later iteration if we light it up).
        try:
            snap = cache.get().data
        except Exception:
            return []
        held = [p.symbol for p in (snap.positions or [])
                if (p.asset_class or "").lower() in ("us_equity", "stock", "")]
        watchlist = [o.symbol for o in (snap.opportunities or [])[:top_n]
                     if (o.asset_class or "").lower() in ("us_equity", "stock", "")]
        seen: set[str] = set()
        out: list[str] = []
        for s in held + watchlist:
            su = (s or "").upper()
            if su and su not in seen:
                seen.add(su)
                out.append(su)
        return out

    loop = asyncio.get_running_loop()
    runner = _md_start(
        settings=Settings(), broadcaster=broadcaster, loop=loop,
        symbol_provider=_provider,
    )
    if runner is None:
        return
    app.state.market_data_runner = runner

    # Listen for symbol-set churn events. Debounce is in the runner;
    # we just trigger it on each relevant event.
    async def _symbol_listener() -> None:
        cq = await broadcaster.subscribe()
        try:
            while True:
                ev = await cq.q.get()
                if ev.type in ("position.changed", "opportunities.updated", "intel.updated"):
                    runner.update_symbols()
        finally:
            await broadcaster.unsubscribe(cq)

    app.state.market_data_listener_task = asyncio.create_task(
        _symbol_listener(), name="market-data-symbol-listener",
    )


def _system_view_ctx() -> dict[str, Any]:
    """Extra context for system.html. Computes node health + last-activity
    once per page load; live updates come from /fragment/system_nodes
    plus SSE flashes for sub-second feedback.
    """
    from trading_bot.dashboard.system_state import build_system_snapshot
    from trading_bot.dashboard import system_topology as topo
    snap = build_system_snapshot(STATE_DB_PATH)
    nodes_by_zone: dict[str, list] = {z: [] for z, _ in topo.ZONES}
    for n in topo.NODES:
        nodes_by_zone[n.zone].append(n)
    return {
        "zones": topo.ZONES,
        "nodes_by_zone": nodes_by_zone,
        "node_health": {nid: info.get("health", "off") for nid, info in snap.items()},
        "node_last":   {nid: info.get("last_activity_label", "") for nid, info in snap.items()},
    }


def _host_ok(request: Request) -> bool:
    """DNS-rebinding defense for streaming endpoints.

    The dashboard binds 127.0.0.1, but a public webpage in another tab
    could try to make its hostname resolve to 127.0.0.1 and then fetch
    /api/stream. Reject anything that doesn't look like localhost. Port
    is allowed to vary so dev servers (8000, 8765, …) still work.
    """
    host_hdr = request.headers.get("host") or ""
    host = host_hdr.split(":", 1)[0].strip().lower()
    return host in _ALLOWED_HOSTS


def _et_filter(value, fmt: str = "%b %d %-I:%M %p ET"):
    """Jinja filter — render any datetime in America/New_York with given format.

    Usage in templates: `{{ dt | et }}` or `{{ dt | et('%H:%M ET') }}`.
    Naive datetimes are assumed UTC (matches state.db storage convention).
    """
    if value is None:
        return ""
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo

    if not isinstance(value, _dt):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=_tz.utc)
    return value.astimezone(ZoneInfo("America/New_York")).strftime(fmt)


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)
    base = Path(__file__).parent
    templates = Jinja2Templates(directory=str(base / "templates"))
    templates.env.filters["et"] = _et_filter
    app.mount("/static", StaticFiles(directory=str(base / "static")), name="static")

    # --- Real-time event bus wiring (Phase 0) ----------------------------
    # The dashboard process is a producer too — it stamps its own emissions
    # with process="dashboard" so cross-process emit counts are visible in
    # /api/stream/health. The Broadcaster runs one tail loop and fans
    # events out to all connected SSE clients.
    _bus_mod.set_process_tag("dashboard")
    broadcaster = Broadcaster(STATE_DB_PATH)
    app.state.broadcaster = broadcaster

    @app.on_event("startup")
    async def _start_broadcaster() -> None:
        await broadcaster.start()
        # Self-emit so the dashboard's own row appears in /api/stream
        # health by_process; also useful as a "is the loop alive" probe.
        try:
            _bus_mod.emit("process.started", {"process": "dashboard"}, source="dashboard")
        except Exception:
            pass
        # Phase 8 — gated live market-data ticks. Off by default.
        await _maybe_start_market_data_stream(app, broadcaster, cache)

    @app.on_event("shutdown")
    async def _stop_broadcaster() -> None:
        runner = getattr(app.state, "market_data_runner", None)
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        sym_listener = getattr(app.state, "market_data_listener_task", None)
        if sym_listener is not None:
            sym_listener.cancel()
        await broadcaster.stop()

    cache = _SnapshotCache(ttl=CACHE_TTL_SECONDS)
    # Build once eagerly so the first request is fast.
    try:
        cache.force_refresh()
    except Exception:
        pass
    _start_background_refresher(cache, BG_REFRESH_INTERVAL)

    def _ctx(combined: _CombinedSnapshot, range_key: str = "1m",
             active_view: str = "operator") -> dict[str, Any]:
        snap = combined.data
        session_code, session_label = _market_session()
        curve = _filter_equity_range(list(snap.equity_curve), range_key)
        return {
            "s": snap,
            "insights": combined.insights,
            "fmt": _formatters(),
            "equity_points": [
                {"ts": p.ts.isoformat(), "equity": float(p.equity)} for p in curve
            ],
            "equity_range": range_key,
            "market_session": {"code": session_code, "label": session_label},
            "fragments": list(FRAGMENTS.keys()),
            "scan_age_seconds": _scan_age_seconds(snap),
            "lab": _lab_views(),
            "active_view": active_view,
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str | None = None) -> Any:
        # Two views share this route. ?view=system|operator wins; else
        # the dash_view cookie persists the last choice; else "operator".
        chosen = (view or request.cookies.get("dash_view") or "operator").lower()
        if chosen not in ("operator", "system"):
            chosen = "operator"
        combined = cache.get()
        ctx = _ctx(combined, active_view=chosen)
        if chosen == "system":
            ctx.update(_system_view_ctx())
            resp = templates.TemplateResponse(request, "system.html", ctx)
        else:
            resp = templates.TemplateResponse(request, "dashboard.html", ctx)
        # Persist the toggle for 30 days.
        if view in ("operator", "system"):
            resp.set_cookie("dash_view", view, max_age=60 * 60 * 24 * 30,
                            samesite="lax", httponly=False)
        return resp

    @app.get("/fragment/portfolio_rail", response_class=HTMLResponse)
    def fragment_portfolio_rail(request: Request) -> Any:
        combined = cache.get()
        return templates.TemplateResponse(request, "_portfolio_rail.html", _ctx(combined))

    @app.get("/fragment/system_nodes")
    def fragment_system_nodes(request: Request) -> Any:
        # JSON snapshot — lightweight, used by system.js to refresh
        # health colors + last-activity timestamps every 30s.
        from trading_bot.dashboard.system_state import build_system_snapshot
        snap = build_system_snapshot(STATE_DB_PATH)
        return JSONResponse({"nodes": snap})

    @app.get("/fragment/node/{node_id}", response_class=HTMLResponse)
    def fragment_node(request: Request, node_id: str) -> Any:
        from trading_bot.dashboard import system_topology as topo
        from trading_bot.dashboard.system_state import build_system_snapshot
        n = topo.node_by_id(node_id)
        if n is None:
            raise HTTPException(status_code=404, detail=f"unknown node: {node_id}")
        # Pull the same data system.js uses, plus richer per-node detail.
        health_all = build_system_snapshot(STATE_DB_PATH)
        health = health_all.get(node_id, {"health": "off", "last_activity_label": ""})
        role_runs = _last_role_runs(STATE_DB_PATH, n.role_name, limit=10) if n.role_name else []
        events = _recent_events_for(STATE_DB_PATH, n.subscribes, limit=20) if n.subscribes else []
        ctx = {
            "node": n,
            "health": health,
            "role_runs": role_runs,
            "events": events,
            "subscribes": list(n.subscribes),
            "fmt": _formatters(),
        }
        return templates.TemplateResponse(request, "_node_drilldown.html", ctx)

    @app.get("/architecture", response_class=HTMLResponse)
    def architecture(request: Request) -> Any:
        return templates.TemplateResponse(request, "architecture.html", {})

    def _persona_card_payload(p: Any) -> dict:
        """Render a Persona dataclass into the dict shape the desk_roster
        template consumes. Adds a ``display_label`` (e.g. 'Sasha Volkov ·
        On-Chain Forensic Analyst, 8yr') and the persona's id so the
        template can build click-to-bio links.
        """
        from trading_bot.shared.personas._base import display_label
        return {
            "id": p.id,
            "full_name": p.full_name,
            "role_title": p.role_title,
            "years_experience": p.years_experience,
            "firm_pedigree": p.firm_pedigree,
            "specialties": list(p.specialties),
            "default_stance": p.default_stance,
            "pipeline": p.pipeline,
            "debate_role": p.debate_role,
            "model_tier": p.model_tier,
            "prompt_version": p.prompt_version,
            "display_label": display_label(p),
        }

    @app.get("/desk", response_class=HTMLResponse)
    def trading_desk_roster(request: Request) -> Any:
        """Phase 1G — Trading Desk Roster page.

        Reads PERSONA dicts from every persona module across
        ``shared/personas/`` and ``pipelines/{stocks,crypto,options}/personas/``
        and renders a roster grouped by pipeline. Each card carries
        per-persona accuracy stats (n_runs, hit-rate %, last-run-at)
        joined from the debate audit tables across pipelines.
        """
        from trading_bot.shared.personas._base import discover, display_label
        from trading_bot.shared.persona_accuracy import compute_persona_stats

        # Compute stats once per request — the dashboard's per-second cache
        # absorbs traffic, and the audit tables stay small (months not
        # years of debate rows).
        stats_by_key: dict[tuple[str, str], Any] = {}
        try:
            from trading_bot.state_db import get_engine as _get_engine
            _state_db = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
            stats_by_key = compute_persona_stats(
                _get_engine(_state_db), lookback_days=30,
            )
        except Exception:
            # Empty fallback so the roster page still renders when the
            # audit tables are unavailable.
            stats_by_key = {}

        groups: dict[str, list[dict]] = {"shared": [], "stocks": [],
                                         "crypto": [], "options": []}

        def _attach_stats(card: dict) -> dict:
            """Merge accuracy payload (or null) into a persona card."""
            key = (card["pipeline"], card["debate_role"])
            stats = stats_by_key.get(key)
            card["accuracy"] = stats.to_dict() if stats is not None else None
            return card

        # shared/personas
        try:
            from trading_bot.shared import personas as shared_personas
            for p in discover(shared_personas):
                groups["shared"].append(_attach_stats(_persona_card_payload(p)))
        except Exception:
            pass

        # crypto pipeline personas
        try:
            from trading_bot.pipelines.crypto import personas as crypto_personas
            for p in discover(crypto_personas):
                groups["crypto"].append(_attach_stats(_persona_card_payload(p)))
        except Exception:
            pass

        # stocks-pipeline personas (lives at trading_bot.personas during
        # the Option-4 hybrid; Option 2 strangler-fig will move it later)
        try:
            from trading_bot import personas as stocks_personas
            for p in discover(stocks_personas):
                groups["stocks"].append(_attach_stats(_persona_card_payload(p)))
        except Exception:
            pass

        # options-pipeline personas (Phase 3 — empty until built)
        try:
            from trading_bot.pipelines.options import personas as options_personas  # type: ignore[import-not-found]
            for p in discover(options_personas):
                groups["options"].append(_attach_stats(_persona_card_payload(p)))
        except Exception:
            pass

        # Sort each pipeline's roster by debate_role for visual stability.
        for k in groups:
            groups[k].sort(key=lambda c: (c["debate_role"], c["full_name"]))

        ctx = {
            "groups": groups,
            "total": sum(len(v) for v in groups.values()),
        }
        return templates.TemplateResponse(request, "desk_roster.html", ctx)

    @app.get("/refresh", response_class=HTMLResponse)
    def refresh(request: Request) -> Any:
        combined = cache.force_refresh()
        return templates.TemplateResponse(request, "dashboard.html", _ctx(combined))

    @app.get("/fragment/{name}", response_class=HTMLResponse)
    def fragment(request: Request, name: str, range: str = "1m") -> Any:
        if name not in FRAGMENTS:
            raise HTTPException(status_code=404, detail=f"unknown fragment: {name}")
        combined = cache.get()
        return templates.TemplateResponse(request, FRAGMENTS[name], _ctx(combined, range))

    @app.get("/api/snapshot")
    def api_snapshot() -> Any:
        combined = cache.get()
        return JSONResponse(_snapshot_to_dict(combined.data))

    @app.get("/api/equity-curve")
    def api_equity_curve(range: str = "1m") -> Any:
        combined = cache.get()
        curve = _filter_equity_range(list(combined.data.equity_curve), range)
        return JSONResponse({
            "range": range,
            "points": [
                {"ts": p.ts.isoformat(), "equity": float(p.equity)} for p in curve
            ],
        })

    @app.get("/api/market-session")
    def api_market_session() -> Any:
        code, label = _market_session()
        return JSONResponse({"code": code, "label": label})

    # ----- Real-time SSE stream + health (Phase 0) ----------------------
    @app.get("/api/stream")
    async def api_stream(request: Request) -> Any:
        if not _host_ok(request):
            raise HTTPException(status_code=403, detail="Host header not allowed")
        # Last-Event-ID resume: the browser sends this on auto-reconnect
        # so we replay missed events for *this* client only.
        last_event_id_hdr = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
        try:
            last_event_id = int(last_event_id_hdr) if last_event_id_hdr else None
        except ValueError:
            last_event_id = None

        async def event_stream():
            cq = await broadcaster.subscribe()
            try:
                # Server-controlled reconnect hint, jittered per-connection
                # so all clients don't reconnect at the same moment after a
                # dashboard restart.
                retry_ms = _SSE_RETRY_BASE_MS + random.randint(0, _SSE_RETRY_JITTER_MS)
                yield f"retry: {retry_ms}\n\n".encode("utf-8")
                # Replay missed events for this client (no impact on others).
                if last_event_id is not None:
                    await broadcaster.replay_for(cq, last_event_id)
                # Initial hello so the browser knows the connection is live
                # and so we can verify the pipe in DevTools immediately.
                hello = Event(
                    id=0, type="stream.hello",
                    payload={"resumed_from": last_event_id},
                    source="dashboard", process="dashboard",
                    created_at=datetime.now(timezone.utc),
                )
                yield hello.to_sse_line().encode("utf-8")

                last_send = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = await asyncio.wait_for(cq.q.get(), timeout=_SSE_HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        # Heartbeat — comment line keeps the connection
                        # alive through proxies and reassures the browser.
                        yield b": hb\n\n"
                        last_send = time.monotonic()
                        continue
                    yield ev.to_sse_line().encode("utf-8")
                    last_send = time.monotonic()
            finally:
                await broadcaster.unsubscribe(cq)

        # SSE-friendly headers: text/event-stream, no caching, no buffering.
        headers = {
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)

    @app.get("/api/stream/health")
    def api_stream_health(request: Request) -> Any:
        if not _host_ok(request):
            raise HTTPException(status_code=403, detail="Host header not allowed")
        b_stats = broadcaster.stats()
        bus_stats = _bus_mod.get_bus(STATE_DB_PATH).stats()
        # Per-process emission counts in last hour — small, cheap.
        per_process: dict[str, int] = {}
        try:
            import sqlite3
            with sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True, timeout=5.0) as conn:
                rows = conn.execute(
                    "SELECT process, COUNT(*) FROM events "
                    "WHERE created_at > datetime('now', '-1 hour') "
                    "GROUP BY process",
                ).fetchall()
                per_process = {r[0]: int(r[1]) for r in rows}
        except Exception:
            pass
        return JSONResponse({
            "clients_connected": b_stats["clients_connected"],
            "cursor": b_stats["cursor"],
            "lag_ms_p99": b_stats["lag_ms_p99"],
            "events_emitted_total": bus_stats["events_emitted_total"],
            "events_dropped_total": bus_stats["events_dropped_total"],
            "events_written_total": bus_stats["events_written_total"],
            "queue_depth": bus_stats["queue_depth"],
            "by_process_last_hour": per_process,
        })

    @app.post("/api/stream/test-tick")
    async def api_stream_test_tick(request: Request, symbol: str, price: float) -> Any:
        """Phase 8 smoke test: inject a synthetic ``price.update`` event
        directly into the broadcaster (bypassing Alpaca) so the operator
        can verify the holdings cell update path without standing up the
        real market-data stream."""
        if not _host_ok(request):
            raise HTTPException(status_code=403, detail="Host header not allowed")
        ev = Event(
            id=0, type="price.update",
            payload={"symbol": symbol.upper(), "price": float(price)},
            source="dashboard.test", process="dashboard",
            created_at=datetime.now(timezone.utc),
        )
        await broadcaster.broadcast(ev)
        return JSONResponse({"emitted": True, "symbol": symbol.upper(), "price": float(price)})

    @app.post("/api/stream/test-emit")
    def api_stream_test_emit(request: Request, type: str = "stream.hello", payload: str = "{}") -> Any:
        """Smoke-test endpoint: emit one event from the dashboard process so
        you can verify end-to-end pipe in DevTools without standing up a
        producer. Localhost-only by Host check + this endpoint is a no-op
        if the bus failed to start (it logs and returns 200)."""
        if not _host_ok(request):
            raise HTTPException(status_code=403, detail="Host header not allowed")
        import json as _json
        try:
            parsed = _json.loads(payload)
        except Exception:
            parsed = {"raw": payload}
        ok = _bus_mod.emit(type, parsed, source="dashboard.test")
        return JSONResponse({"emitted": ok, "type": type})

    return app


# ---------- helpers exposed to templates ---------------------------------


def _lab_views() -> dict[str, Any]:
    """Pull all Phase 1-6 state.db views in one call. Fast (microseconds)
    because state.db is local SQLite — no caching needed.

    Returns a dict mapping each view name → dataclass (or list). Templates
    reference this as `lab.strategy_mode`, `lab.halts`, etc.
    """
    import os as _os

    from sqlalchemy.orm import Session as _Session

    from trading_bot import lab_data
    from trading_bot.state_db import get_engine as _get_engine

    state_db = _os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
    try:
        engine = _get_engine(state_db)
        with _Session(engine) as s:
            return {
                "strategy_mode": lab_data.strategy_mode(s),
                "hold_spy_transition": lab_data.hold_spy_transition(s),
                "halts": lab_data.active_halts(s),
                "lab_evolution": lab_data.lab_evolution(s),
                "calibrator": lab_data.calibrator(s),
                "llm_spend": lab_data.llm_spend(s),
                "role_health": lab_data.role_health(s),
                "proposals": lab_data.recent_proposals(s, limit=5),
                "threshold_overrides": lab_data.threshold_overrides(s),
                "intel_pool": lab_data.intel_pool(s),
            }
    except Exception:
        # Empty fallback so templates never crash on missing schema.
        return {
            "strategy_mode": None,
            "hold_spy_transition": None,
            "halts": [],
            "lab_evolution": None,
            "calibrator": None,
            "llm_spend": None,
            "role_health": [],
            "proposals": [],
            "threshold_overrides": None,
            "intel_pool": None,
        }


def _scan_age_seconds(snap: DashboardSnapshot) -> int | None:
    if snap.last_scan is None:
        return None
    ts = snap.last_scan.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - ts).total_seconds())


def _formatters() -> dict[str, Any]:
    """Helpers exposed to the template as `fmt.X`."""
    from decimal import Decimal

    def usd(v: Any, sign: bool = False) -> str:
        try:
            d = Decimal(str(v))
        except Exception:
            return "—"
        s = "+" if sign and d > 0 else ""
        return f"{s}${d:,.2f}"

    def pct(v: Any, sign: bool = False) -> str:
        try:
            d = Decimal(str(v))
        except Exception:
            return "—"
        s = "+" if sign and d > 0 else ""
        return f"{s}{d:.2f}%"

    def num_or_dash(v: Any, suffix: str = "") -> str:
        if v is None:
            return "—"
        return f"{v}{suffix}"

    def regime_color(r: str) -> str:
        return {
            "trending_up": "emerald",
            "trending_down": "rose",
            "sideways": "amber",
            "risk_off": "rose",
        }.get(r, "slate")

    def pnl_color(v: Any) -> str:
        try:
            d = float(str(v))
        except Exception:
            return "slate"
        if d > 0:
            return "emerald"
        if d < 0:
            return "rose"
        return "slate"

    def clamp_pct(v: Any) -> float:
        """Clamp 0..100 for width styling; tolerates None/NaN."""
        try:
            x = float(v)
        except Exception:
            return 0.0
        if x != x:  # NaN
            return 0.0
        return max(0.0, min(100.0, x))

    def short_dt(v: Any) -> str:
        """Locale-portable short date/time. Avoids %-I/%-d (GNU/BSD only)."""
        try:
            if isinstance(v, datetime):
                d = v
            else:
                d = datetime.fromisoformat(str(v))
        except Exception:
            return "—"
        s = d.strftime("%b %d at %I:%M %p UTC")
        return s.replace(" 0", " ")  # strip leading zeros portably

    def relative_age(seconds: Any) -> str:
        try:
            n = int(seconds)
        except Exception:
            return "—"
        if n < 60:
            return f"{n}s ago"
        if n < 3600:
            return f"{n // 60}m ago"
        if n < 86400:
            return f"{n // 3600}h {(n % 3600) // 60}m ago"
        return f"{n // 86400}d ago"

    class _Fmt:
        pass

    f = _Fmt()
    f.usd = usd
    f.pct = pct
    f.num_or_dash = num_or_dash
    f.regime_color = regime_color
    f.pnl_color = pnl_color
    f.clamp_pct = clamp_pct
    f.short_dt = short_dt
    f.relative_age = relative_age
    return f  # type: ignore[return-value]


def _snapshot_to_dict(s: DashboardSnapshot) -> dict[str, Any]:
    return {
        "generated_at": s.generated_at.isoformat(),
        "regime": s.regime,
        "regime_notes": s.regime_notes,
        "vix": s.vix,
        "vol_pct": s.vol_pct,
        "automation_status": s.automation_status,
        "automation_note": s.automation_note,
        "kpi": {
            "equity": float(s.kpi.equity),
            "cash": float(s.kpi.cash),
            "cash_pct": float(s.kpi.cash_pct),
            "invested_pct": float(s.kpi.invested_pct),
            "open_pnl": float(s.kpi.open_pnl),
            "today_pnl_pct": float(s.kpi.today_pnl_pct),
            "max_drawdown_pct": float(s.kpi.max_drawdown_pct),
            "open_position_count": s.kpi.open_position_count,
        },
        "stats": {
            "total_trades": s.stats.total_trades,
            "wins": s.stats.wins,
            "losses": s.stats.losses,
            "win_rate_pct": s.stats.win_rate_pct,
            "profit_factor": s.stats.profit_factor,
            "avg_rr": s.stats.avg_rr,
            "expectancy": float(s.stats.expectancy) if s.stats.expectancy else None,
            "best_trade": float(s.stats.best_trade) if s.stats.best_trade else None,
            "best_trade_symbol": s.stats.best_trade_symbol,
            "worst_trade": float(s.stats.worst_trade) if s.stats.worst_trade else None,
            "worst_trade_symbol": s.stats.worst_trade_symbol,
            "avg_win": float(s.stats.avg_win) if s.stats.avg_win else None,
            "avg_loss": float(s.stats.avg_loss) if s.stats.avg_loss else None,
            "streak": s.stats.streak,
        },
        "positions": [
            {"symbol": p.symbol, "asset_class": p.asset_class,
             "qty": float(p.qty), "avg_entry": float(p.avg_entry),
             "last_price": float(p.last_price),
             "market_value": float(p.market_value),
             "unrealized_pl": float(p.unrealized_pl),
             "unrealized_pl_pct": float(p.unrealized_pl_pct)}
            for p in s.positions
        ],
        "orders": [
            {"symbol": o.symbol, "side": o.side, "qty": o.qty,
             "type": o.order_type, "status": o.status,
             "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None}
            for o in s.orders
        ],
        "opportunities": [
            {"rank": o.rank, "symbol": o.symbol, "asset_class": o.asset_class}
            for o in s.opportunities
        ],
        "exposure": [
            {"bucket": e.bucket, "pct": float(e.pct), "value": float(e.value)}
            for e in s.exposure
        ],
        "universe_size": s.universe_size,
        "universe_source": s.universe_source,
        "errors": s.errors,
        "market_session": dict(zip(("code", "label"), _market_session())),
    }


# Module-level FastAPI app — exposed so callers can do
#   `from trading_bot.dashboard.app import app`
# (used by tests + ASGI workers that import the variable directly).
# Built lazily; if construction fails (e.g. missing settings during import),
# a minimal stub app is provided so imports don't crash.
try:
    app = create_app()
except Exception:  # pragma: no cover — defensive
    app = FastAPI(title="Trading Bot Dashboard (stub)")


def run(host: str = "127.0.0.1", port: int = 8765, reload: bool = False, workers: int = 1) -> None:
    """Start uvicorn. Bound to localhost only — no auth.

    ``workers`` MUST be 1: the SSE broadcaster keeps per-client queues in
    memory inside one process, and clients pinned to a different worker
    would never see events broadcast on the first one. The assertion is
    here at the entry point, not inside ``create_app`` (where a worker
    can't introspect its own count).
    """
    if workers != 1:
        raise RuntimeError(
            "Trading Bot Dashboard requires workers=1 — the SSE broadcaster "
            "uses in-process fan-out, multi-worker would silently drop events. "
            "Run with workers=1 or split the streaming endpoint to a separate service."
        )
    import uvicorn

    uvicorn.run(
        "trading_bot.dashboard.app:create_app",
        host=host, port=port, reload=reload, factory=True,
        log_level="info", workers=1,
    )
