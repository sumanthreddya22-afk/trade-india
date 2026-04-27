"""FastAPI app for the local trading dashboard.

Single page (`GET /`). Auto-refreshes via HTMX `hx-get="/"` every 60s,
swapping the entire `<main>` body. The snapshot is cached server-side
for 30s so 10+ concurrent section reloads don't hammer Alpaca.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from trading_bot.config import Settings, load_config
from trading_bot.dashboard.data import DashboardSnapshot, build_snapshot

CONFIG_PATH = Path("strategy/config.yaml")
WATCHLIST_PATH = Path("strategy/watchlist.yaml")
OPPORTUNITIES_PATH = Path("strategy/opportunities.md")
CLOSED_DB_PATH = Path("data/closed_trades.db")

CACHE_TTL_SECONDS = 30


class _SnapshotCache:
    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._stamp: float = 0.0
        self._snap: DashboardSnapshot | None = None

    def get(self) -> DashboardSnapshot:
        now = time.time()
        if self._snap is None or (now - self._stamp) > self._ttl:
            self._snap = build_snapshot(
                settings=Settings(),
                config=load_config(CONFIG_PATH),
                opportunities_path=OPPORTUNITIES_PATH,
                watchlist_path=WATCHLIST_PATH,
                closed_db_path=CLOSED_DB_PATH,
            )
            self._stamp = now
        return self._snap

    def invalidate(self) -> None:
        self._snap = None
        self._stamp = 0.0


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)
    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    cache = _SnapshotCache(ttl=CACHE_TTL_SECONDS)

    def _ctx(snap: DashboardSnapshot) -> dict[str, Any]:
        return {
            "s": snap,
            "fmt": _formatters(),
            "equity_points": [
                {"ts": p.ts.isoformat(), "equity": float(p.equity)}
                for p in snap.equity_curve
            ],
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
        cache.invalidate()
        snap = cache.get()
        return templates.TemplateResponse(request, "dashboard.html", _ctx(snap))

    @app.get("/api/snapshot")
    def api_snapshot() -> Any:
        snap = cache.get()
        return JSONResponse(_snapshot_to_dict(snap))

    @app.get("/api/equity-curve")
    def api_equity_curve() -> Any:
        snap = cache.get()
        return JSONResponse({
            "points": [
                {"ts": p.ts.isoformat(), "equity": float(p.equity)}
                for p in snap.equity_curve
            ],
        })

    return app


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

    class _Fmt:
        pass

    f = _Fmt()
    f.usd = usd
    f.pct = pct
    f.num_or_dash = num_or_dash
    f.regime_color = regime_color
    f.pnl_color = pnl_color
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
    }


def run(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    """Start uvicorn. Bound to localhost only — no auth."""
    import uvicorn

    uvicorn.run(
        "trading_bot.dashboard.app:create_app",
        host=host, port=port, reload=reload, factory=True,
        log_level="info",
    )
