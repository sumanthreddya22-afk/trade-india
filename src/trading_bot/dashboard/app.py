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

import threading
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trading_bot.config import Settings, load_config
from trading_bot.dashboard.data import DashboardSnapshot, build_snapshot

CONFIG_PATH = Path("strategy/config.yaml")
WATCHLIST_PATH = Path("strategy/watchlist.yaml")
OPPORTUNITIES_PATH = Path("strategy/opportunities.md")
CLOSED_DB_PATH = Path("data/closed_trades.db")

CACHE_TTL_SECONDS = 25
BG_REFRESH_INTERVAL = 25

# Whitelist of fragment names → partial template files.
FRAGMENTS: dict[str, str] = {
    "header": "_header.html",
    "regime": "_regime.html",
    "kpi": "_kpi.html",
    "risk": "_risk.html",
    "macro_alloc": "_macro_alloc.html",
    "last_scan": "_last_scan.html",
    "exposure": "_exposure.html",
    "equity": "_equity.html",
    "stats": "_stats.html",
    "opportunities": "_opportunities.html",
    "orders": "_orders.html",
    "scheduled": "_scheduled.html",
    "errors": "_errors.html",
    "sidebar_status": "_sidebar_status.html",
    # Phase 1-6 surfaces
    "strategy_mode": "_strategy_mode.html",
    "halts": "_halts.html",
    "lab_evolution": "_lab_evolution.html",
    "calibrator": "_calibrator.html",
    "llm_spend": "_llm_spend.html",
    "role_health": "_role_health.html",
    "proposals": "_proposals.html",
}


class _SnapshotCache:
    """Thread-safe snapshot cache with single-flight semantics."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._stamp: float = 0.0
        self._snap: DashboardSnapshot | None = None
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()

    def _is_fresh(self, now: float) -> bool:
        return self._snap is not None and (now - self._stamp) <= self._ttl

    def get(self) -> DashboardSnapshot:
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

    def force_refresh(self) -> DashboardSnapshot:
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

    def _build(self) -> DashboardSnapshot:
        return build_snapshot(
            settings=Settings(),
            config=load_config(CONFIG_PATH),
            opportunities_path=OPPORTUNITIES_PATH,
            watchlist_path=WATCHLIST_PATH,
            closed_db_path=CLOSED_DB_PATH,
        )


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

    cache = _SnapshotCache(ttl=CACHE_TTL_SECONDS)
    # Build once eagerly so the first request is fast.
    try:
        cache.force_refresh()
    except Exception:
        pass
    _start_background_refresher(cache, BG_REFRESH_INTERVAL)

    def _ctx(snap: DashboardSnapshot, range_key: str = "1m") -> dict[str, Any]:
        session_code, session_label = _market_session()
        curve = _filter_equity_range(list(snap.equity_curve), range_key)
        return {
            "s": snap,
            "fmt": _formatters(),
            "equity_points": [
                {"ts": p.ts.isoformat(), "equity": float(p.equity)} for p in curve
            ],
            "equity_range": range_key,
            "market_session": {"code": session_code, "label": session_label},
            "fragments": list(FRAGMENTS.keys()),
            "scan_age_seconds": _scan_age_seconds(snap),
            "lab": _lab_views(),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        snap = cache.get()
        return templates.TemplateResponse(request, "dashboard.html", _ctx(snap))

    @app.get("/architecture", response_class=HTMLResponse)
    def architecture(request: Request) -> Any:
        return templates.TemplateResponse(request, "architecture.html", {})

    @app.get("/refresh", response_class=HTMLResponse)
    def refresh(request: Request) -> Any:
        snap = cache.force_refresh()
        return templates.TemplateResponse(request, "dashboard.html", _ctx(snap))

    @app.get("/fragment/{name}", response_class=HTMLResponse)
    def fragment(request: Request, name: str, range: str = "1m") -> Any:
        if name not in FRAGMENTS:
            raise HTTPException(status_code=404, detail=f"unknown fragment: {name}")
        snap = cache.get()
        return templates.TemplateResponse(request, FRAGMENTS[name], _ctx(snap, range))

    @app.get("/api/snapshot")
    def api_snapshot() -> Any:
        snap = cache.get()
        return JSONResponse(_snapshot_to_dict(snap))

    @app.get("/api/equity-curve")
    def api_equity_curve(range: str = "1m") -> Any:
        snap = cache.get()
        curve = _filter_equity_range(list(snap.equity_curve), range)
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


def run(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    """Start uvicorn. Bound to localhost only — no auth."""
    import uvicorn

    uvicorn.run(
        "trading_bot.dashboard.app:create_app",
        host=host, port=port, reload=reload, factory=True,
        log_level="info",
    )
