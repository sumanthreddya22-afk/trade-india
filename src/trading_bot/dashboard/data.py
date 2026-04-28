"""Dashboard snapshot builder.

Aggregates from: AlpacaClient, regime detector, intelligence (FRED VIX),
opportunities.md, trade_journal, closed_trades store, portfolio snapshots.
Returns a single DashboardSnapshot the template renders.

Designed to never raise — every section degrades to "—" / empty list on
upstream failure, since the dashboard runs against a live (and sometimes
flaky) external API.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest

from trading_bot.alpaca_client import AlpacaClient
from trading_bot.config import AppConfig, Settings
from trading_bot.intelligence import get_macro_snapshot
from trading_bot.last_scan import PersistedScan, read_last_scan
from trading_bot.market_data import MarketDataClient
from trading_bot.orchestrator import load_ranked_watchlist
from trading_bot.pnl_state import PnlStateBuilder
from trading_bot.reconciliation import ClosedTrade, ClosedTradeStore
from trading_bot.regime import detect_regime
from trading_bot.state import load_watchlist


@dataclass(frozen=True)
class KpiBlock:
    equity: Decimal
    cash: Decimal
    cash_pct: Decimal
    invested_pct: Decimal
    open_pnl: Decimal
    today_pnl_pct: Decimal
    max_drawdown_pct: Decimal
    open_position_count: int


@dataclass(frozen=True)
class StatsBlock:
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float | None
    profit_factor: float | None
    avg_rr: float | None
    expectancy: Decimal | None
    best_trade: Decimal | None
    best_trade_symbol: str
    worst_trade: Decimal | None
    worst_trade_symbol: str
    avg_win: Decimal | None
    avg_loss: Decimal | None
    streak: str  # e.g. "3W" or "2L" or "—"


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    asset_class: str
    qty: Decimal
    avg_entry: Decimal
    last_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal
    unrealized_pl_pct: Decimal


@dataclass(frozen=True)
class OrderRow:
    symbol: str
    side: str
    qty: str
    order_type: str
    status: str
    submitted_at: datetime | None


@dataclass(frozen=True)
class OpportunityRow:
    rank: int
    symbol: str
    asset_class: str


@dataclass(frozen=True)
class EquityPoint:
    ts: datetime
    equity: Decimal


@dataclass(frozen=True)
class ExposureRow:
    bucket: str  # "stock" | "crypto" | "options" | "cash"
    pct: Decimal
    value: Decimal


@dataclass(frozen=True)
class RiskHeadroomRow:
    rule: str          # human label
    used_pct: float    # 0-100 of cap
    note: str          # "X / Y%" or "X loses days / cap"
    severity: str      # "ok" | "warn" | "halt"


@dataclass(frozen=True)
class HaltStatusBlock:
    halted: bool
    reason: str
    daily_pnl_pct: Decimal
    weekly_pnl_pct: Decimal
    consecutive_losing_days: int


@dataclass(frozen=True)
class ScheduledJobRow:
    task_id: str
    label: str
    cron: str
    next_run_local: str
    fires_per_day_estimate: str


@dataclass(frozen=True)
class MacroBlock:
    vix: float | None
    yield_10y_pct: float | None
    fed_funds_pct: float | None


@dataclass(frozen=True)
class AllocationDriftRow:
    bucket: str
    target_pct: Decimal
    actual_pct: Decimal
    drift_pct: Decimal  # actual - target


@dataclass(frozen=True)
class DecisionRow:
    symbol: str
    action: str
    reason: str
    badge_color: str  # "emerald" placed | "rose" rejected | "amber" hold | "slate" other


@dataclass(frozen=True)
class LastScanBlock:
    command: str
    regime: str
    universe_size: int
    timestamp: datetime
    placed: int
    rejected: int
    holds: int
    decisions: list[DecisionRow]


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: datetime
    regime: str
    regime_notes: str
    vix: float | None
    vol_pct: float
    automation_status: str  # "OK" | "WARN" | "DOWN"
    automation_note: str
    kpi: KpiBlock
    stats: StatsBlock
    positions: list[PositionRow]
    orders: list[OrderRow]
    opportunities: list[OpportunityRow]
    exposure: list[ExposureRow]
    equity_curve: list[EquityPoint]
    universe_size: int
    universe_source: str  # "opportunities.md" or "watchlist.yaml (fallback)"
    risk_headroom: list[RiskHeadroomRow] = field(default_factory=list)
    halt: HaltStatusBlock | None = None
    scheduled_jobs: list[ScheduledJobRow] = field(default_factory=list)
    macro: MacroBlock | None = None
    allocation_drift: list[AllocationDriftRow] = field(default_factory=list)
    last_scan: LastScanBlock | None = None
    errors: list[str] = field(default_factory=list)


# ---------- helpers ----------


def _safe_decimal(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _empty_kpi() -> KpiBlock:
    z = Decimal("0")
    return KpiBlock(
        equity=z, cash=z, cash_pct=z, invested_pct=z,
        open_pnl=z, today_pnl_pct=z, max_drawdown_pct=z,
        open_position_count=0,
    )


def _empty_stats() -> StatsBlock:
    return StatsBlock(
        total_trades=0, wins=0, losses=0,
        win_rate_pct=None, profit_factor=None, avg_rr=None,
        expectancy=None,
        best_trade=None, best_trade_symbol="",
        worst_trade=None, worst_trade_symbol="",
        avg_win=None, avg_loss=None,
        streak="—",
    )


def _build_kpi(alpaca: AlpacaClient, errors: list[str]) -> tuple[KpiBlock, list[PositionRow]]:
    try:
        account = alpaca.get_account()
        positions = alpaca.get_positions()
    except Exception as e:
        errors.append(f"alpaca account/positions: {e}")
        return _empty_kpi(), []

    equity = _safe_decimal(account.equity)
    cash = _safe_decimal(account.cash)
    invested = max(equity - cash, Decimal("0"))
    cash_pct = (cash / equity * 100).quantize(Decimal("0.01")) if equity > 0 else Decimal("0")
    invested_pct = (invested / equity * 100).quantize(Decimal("0.01")) if equity > 0 else Decimal("0")

    rows: list[PositionRow] = []
    open_pnl = Decimal("0")
    for p in positions:
        try:
            ue = _safe_decimal(p.unrealized_pl)
            open_pnl += ue
            avg_e = _safe_decimal(p.avg_entry_price)
            qty = _safe_decimal(p.qty)
            mv = _safe_decimal(p.market_value)
            # Local Position dataclass doesn't expose last/current price;
            # derive it from market_value / qty (signed for shorts).
            last = (mv / qty) if qty != 0 else avg_e
            pct = ((last / avg_e - 1) * 100).quantize(Decimal("0.01")) if avg_e > 0 else Decimal("0")
            rows.append(PositionRow(
                symbol=p.symbol,
                asset_class=str(p.asset_class),
                qty=qty,
                avg_entry=avg_e, last_price=last,
                market_value=mv, unrealized_pl=ue, unrealized_pl_pct=pct,
            ))
        except Exception as e:
            errors.append(f"position {getattr(p, 'symbol', '?')}: {e}")
            continue

    return KpiBlock(
        equity=equity, cash=cash,
        cash_pct=cash_pct, invested_pct=invested_pct,
        open_pnl=open_pnl,
        today_pnl_pct=Decimal("0"),  # filled in by curve
        max_drawdown_pct=Decimal("0"),  # filled in by curve
        open_position_count=len(rows),
    ), rows


def _build_equity_curve(
    settings: Settings, errors: list[str]
) -> tuple[list[EquityPoint], Decimal, Decimal]:
    """Returns (curve, today_pnl_pct, max_drawdown_pct)."""
    try:
        client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )
        req = GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        hist = client.get_portfolio_history(req)
    except Exception as e:
        errors.append(f"portfolio_history: {e}")
        return [], Decimal("0"), Decimal("0")

    eq = list(hist.equity or [])
    ts = list(hist.timestamp or [])
    if not eq or not ts or len(eq) != len(ts):
        return [], Decimal("0"), Decimal("0")

    points: list[EquityPoint] = []
    for t, v in zip(ts, eq):
        if v is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(t), tz=timezone.utc)
        except Exception:
            continue
        points.append(EquityPoint(ts=dt, equity=_safe_decimal(v)))

    if len(points) < 2:
        return points, Decimal("0"), Decimal("0")

    today_pct = ((points[-1].equity / points[-2].equity - 1) * 100).quantize(Decimal("0.01"))

    peak = points[0].equity
    max_dd = Decimal("0")
    for p in points:
        if p.equity > peak:
            peak = p.equity
        if peak > 0:
            dd = (p.equity / peak - 1) * 100
            if dd < max_dd:
                max_dd = dd
    max_dd = max_dd.quantize(Decimal("0.01"))

    return points, today_pct, max_dd


def _build_stats(closed: list[ClosedTrade]) -> StatsBlock:
    if not closed:
        return _empty_stats()

    wins = [t for t in closed if t.realized_pnl > 0]
    losses = [t for t in closed if t.realized_pnl < 0]
    n = len(closed)
    nw, nl = len(wins), len(losses)

    win_rate = (nw / n * 100) if n else None
    gross_win = sum((t.realized_pnl for t in wins), Decimal("0"))
    gross_loss = abs(sum((t.realized_pnl for t in losses), Decimal("0")))
    pf: float | None
    if gross_loss > 0:
        pf = float(gross_win / gross_loss)
    elif gross_win > 0:
        pf = float("inf")
    else:
        pf = None

    avg_win = (gross_win / nw).quantize(Decimal("0.01")) if nw else None
    avg_loss = (-gross_loss / nl).quantize(Decimal("0.01")) if nl else None

    avg_rr: float | None = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        avg_rr = float(avg_win / abs(avg_loss))

    expectancy: Decimal | None = None
    if n:
        expectancy = (sum((t.realized_pnl for t in closed), Decimal("0")) / n).quantize(
            Decimal("0.01")
        )

    best = max(closed, key=lambda t: t.realized_pnl)
    worst = min(closed, key=lambda t: t.realized_pnl)

    # Streak from chronological order (closed is already ordered by exit_time)
    streak_n = 1
    last_sign = 1 if closed[-1].realized_pnl > 0 else (-1 if closed[-1].realized_pnl < 0 else 0)
    for t in reversed(closed[:-1]):
        s = 1 if t.realized_pnl > 0 else (-1 if t.realized_pnl < 0 else 0)
        if s == last_sign and s != 0:
            streak_n += 1
        else:
            break
    streak = f"{streak_n}{'W' if last_sign > 0 else ('L' if last_sign < 0 else '—')}"

    return StatsBlock(
        total_trades=n, wins=nw, losses=nl,
        win_rate_pct=round(win_rate, 1) if win_rate is not None else None,
        profit_factor=round(pf, 2) if pf is not None and pf != float("inf") else pf,
        avg_rr=round(avg_rr, 2) if avg_rr is not None else None,
        expectancy=expectancy,
        best_trade=best.realized_pnl, best_trade_symbol=best.symbol,
        worst_trade=worst.realized_pnl, worst_trade_symbol=worst.symbol,
        avg_win=avg_win, avg_loss=avg_loss,
        streak=streak,
    )


def _build_orders(settings: Settings, errors: list[str]) -> list[OrderRow]:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )
        orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50))
    except Exception as e:
        errors.append(f"orders: {e}")
        return []

    rows: list[OrderRow] = []
    for o in orders:
        try:
            rows.append(OrderRow(
                symbol=str(o.symbol),
                side=str(o.side).split(".")[-1].lower(),
                qty=str(o.qty),
                order_type=str(o.order_type).split(".")[-1].lower(),
                status=str(o.status).split(".")[-1].lower(),
                submitted_at=getattr(o, "submitted_at", None),
            ))
        except Exception:
            continue
    return rows


def _build_opportunities(opp_path: Path) -> list[OpportunityRow]:
    entries = load_ranked_watchlist(opp_path)
    return [
        OpportunityRow(rank=i + 1, symbol=e.symbol, asset_class=e.asset_class)
        for i, e in enumerate(entries[:25])
    ]


def _build_exposure(
    positions: list[PositionRow], equity: Decimal, cash: Decimal
) -> list[ExposureRow]:
    by_bucket: dict[str, Decimal] = {"stock": Decimal("0"), "crypto": Decimal("0")}
    for p in positions:
        bucket = "crypto" if "crypto" in p.asset_class.lower() else "stock"
        by_bucket[bucket] += p.market_value

    rows: list[ExposureRow] = []
    for bucket in ("stock", "crypto"):
        v = by_bucket[bucket]
        pct = (v / equity * 100).quantize(Decimal("0.01")) if equity > 0 else Decimal("0")
        rows.append(ExposureRow(bucket=bucket, pct=pct, value=v))

    cash_pct = (cash / equity * 100).quantize(Decimal("0.01")) if equity > 0 else Decimal("0")
    rows.append(ExposureRow(bucket="cash", pct=cash_pct, value=cash))
    return rows


def _build_universe_meta(opp_path: Path, watchlist_path: Path) -> tuple[int, str]:
    ranked = load_ranked_watchlist(opp_path)
    if ranked:
        return len(ranked), "opportunities.md"
    fallback = load_watchlist(watchlist_path)
    return len(fallback), "watchlist.yaml (fallback)"


# ---- Tier 1 expansions ---------------------------------------------------


# Cron expressions are interpreted in America/New_York (ET). Keep in sync
# with scheduler_jobs.py (daemon) and lab.py (lab process).
_KNOWN_SCHEDULED_JOBS: list[tuple[str, str, str]] = [
    # ─ Daemon ─────────────────────────────────────────────────────────────
    ("massive_refresh", "Universe refresh (whole-market Polygon scan)", "30 6 * * 1-5"),
    ("premarket_rank", "Pre-market rank → opportunities.md", "30 7 * * 1-5"),
    ("midday_rerank", "Midday rerank (catches morning breakouts)", "0 12 * * 1-5"),
    ("stock_scanner", "Stock scanner (signals + orders)", "30 9-15 * * 1-5"),
    ("crypto_scanner", "Crypto scanner (24/7)", "*/30 * * * *"),
    ("portfolio_monitor", "Portfolio monitor (hourly alerts)", "0 9-16 * * 1-5"),
    ("order_steward_sweep", "Order steward sweep (verify stops)", "0 9-16 * * 1-5"),
    ("vip_listener", "VIP listener (Truth Social / news)", "*/30 9-16 * * 1-5"),
    ("news_warm_morning", "News sentiment warm (pre-open)", "45 8 * * 1-5"),
    ("news_warm_midday", "News sentiment warm (mid + late session)", "45 11,14 * * 1-5"),
    ("strategy_coach", "Strategy Coach (alpha-vs-SPY check)", "0 6 * * 1-5"),
    ("hold_spy_coordinator", "Hold-SPY Coordinator (transition mgmt)", "55 15 * * 1-5"),
    ("midday_report", "Midday rich-report email", "31 12 * * 1-5"),
    ("daily_digest", "EOD digest email", "0 18 * * 1-5"),
    ("log_rotation", "Weekly log rotation", "0 3 * * 0"),
    ("heartbeat", "Heartbeat (every 60s)", "* * * * *"),
    # ─ Lab ────────────────────────────────────────────────────────────────
    ("param_search", "Lab — nightly param search (optuna)", "0 2 * * *"),
    ("auto_promote", "Lab — auto-promote winning variant", "45 2 * * *"),
    ("calibrate", "Lab — calibrator (backtest vs paper drift)", "0 5 * * *"),
    ("saturday_evolve", "Lab — Architect → Reviewer (LLM, weekly)", "0 6 * * 6"),
]


def _build_scheduled_jobs(errors: list[str]) -> list[ScheduledJobRow]:
    """All cron expressions are interpreted in America/New_York (ET).
    Returned `next_run_local` strings are formatted as `Tue 12:00 PM ET`."""
    try:
        from croniter import croniter
    except Exception as e:
        errors.append(f"croniter unavailable: {e}")
        return []
    try:
        from zoneinfo import ZoneInfo
    except Exception as e:
        errors.append(f"zoneinfo unavailable: {e}")
        return []

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    out: list[ScheduledJobRow] = []
    for task_id, label, cron in _KNOWN_SCHEDULED_JOBS:
        try:
            it = croniter(cron, now_et)
            nxt = it.get_next(datetime)
            # croniter returns aware-or-naive matching the input; force ET
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=et)
            label_str = nxt.astimezone(et).strftime("%a %-I:%M %p ET")
            # estimate fires/day by counting next 24h matches
            count = 0
            cur = now_et
            cit = croniter(cron, cur)
            for _ in range(500):
                cur = cit.get_next(datetime)
                if cur.tzinfo is None:
                    cur = cur.replace(tzinfo=et)
                if (cur - now_et).total_seconds() > 86400:
                    break
                count += 1
            est = f"~{count}/day" if count else "—"
        except Exception as e:
            errors.append(f"cron {task_id}: {e}")
            label_str = "—"
            est = "—"
        out.append(ScheduledJobRow(
            task_id=task_id, label=label, cron=cron,
            next_run_local=label_str, fires_per_day_estimate=est,
        ))
    return out


def _build_risk_state_and_headroom(
    settings: Settings, config: AppConfig, kpi: KpiBlock,
    positions: list[PositionRow], errors: list[str],
) -> tuple[HaltStatusBlock | None, list[RiskHeadroomRow]]:
    try:
        builder = PnlStateBuilder(settings, config)
        reading = builder.read()
    except Exception as e:
        errors.append(f"risk state: {e}")
        return None, []

    halt = HaltStatusBlock(
        halted=reading.halted,
        reason=reading.halt_reason or ("none" if not reading.halted else "halted"),
        daily_pnl_pct=reading.daily_pnl_pct,
        weekly_pnl_pct=reading.weekly_pnl_pct,
        consecutive_losing_days=reading.consecutive_losing_days,
    )

    rules: list[RiskHeadroomRow] = []
    risk_cfg = config.risk

    # Daily loss limit
    daily_used = (
        max(float(-reading.daily_pnl_pct) / risk_cfg.daily_loss_limit_pct * 100, 0.0)
        if risk_cfg.daily_loss_limit_pct
        else 0.0
    )
    rules.append(RiskHeadroomRow(
        rule="Daily loss limit",
        used_pct=min(daily_used, 100.0),
        note=f"P&L {reading.daily_pnl_pct:.2f}% / limit -{risk_cfg.daily_loss_limit_pct}%",
        severity=_severity(daily_used),
    ))

    # Weekly loss limit
    weekly_used = (
        max(float(-reading.weekly_pnl_pct) / risk_cfg.weekly_loss_limit_pct * 100, 0.0)
        if risk_cfg.weekly_loss_limit_pct
        else 0.0
    )
    rules.append(RiskHeadroomRow(
        rule="Weekly loss limit",
        used_pct=min(weekly_used, 100.0),
        note=f"P&L {reading.weekly_pnl_pct:.2f}% / limit -{risk_cfg.weekly_loss_limit_pct}%",
        severity=_severity(weekly_used),
    ))

    # Consecutive losing days
    cld_cap = risk_cfg.max_consecutive_losing_days
    cld_used = (
        reading.consecutive_losing_days / cld_cap * 100
        if cld_cap else 0.0
    )
    rules.append(RiskHeadroomRow(
        rule="Consecutive losing days",
        used_pct=min(cld_used, 100.0),
        note=f"{reading.consecutive_losing_days} / {cld_cap} cap",
        severity=_severity(cld_used),
    ))

    # Largest current symbol concentration
    largest = Decimal("0")
    largest_sym = ""
    for p in positions:
        if kpi.equity > 0:
            pct = (p.market_value / kpi.equity * 100)
            if pct > largest:
                largest = pct
                largest_sym = p.symbol
    conc_cap = risk_cfg.max_symbol_concentration_pct
    conc_used = float(largest) / conc_cap * 100 if conc_cap else 0.0
    rules.append(RiskHeadroomRow(
        rule="Symbol concentration",
        used_pct=min(conc_used, 100.0),
        note=(f"{largest_sym} {largest:.2f}% / cap {conc_cap}%" if largest_sym
              else f"none used / cap {conc_cap}%"),
        severity=_severity(conc_used),
    ))

    # Largest current position pct
    pos_used = float(largest) / risk_cfg.max_position_pct * 100 if risk_cfg.max_position_pct else 0.0
    rules.append(RiskHeadroomRow(
        rule="Max position pct",
        used_pct=min(pos_used, 100.0),
        note=f"largest {largest:.2f}% / cap {risk_cfg.max_position_pct}%",
        severity=_severity(pos_used),
    ))

    return halt, rules


def _severity(used_pct: float) -> str:
    if used_pct >= 100:
        return "halt"
    if used_pct >= 70:
        return "warn"
    return "ok"


def _build_macro(errors: list[str]) -> MacroBlock | None:
    try:
        snap = get_macro_snapshot()
        return MacroBlock(
            vix=snap.vix,
            yield_10y_pct=snap.yield_10y_pct,
            fed_funds_pct=snap.fed_funds_pct,
        )
    except Exception as e:
        errors.append(f"macro: {e}")
        return None


def _build_allocation_drift(
    config: AppConfig, regime: str, exposure: list[ExposureRow]
) -> list[AllocationDriftRow]:
    target = config.regime_allocations.get(regime)
    if target is None:
        return []
    actual_by = {e.bucket: e.pct for e in exposure}
    rows: list[AllocationDriftRow] = []
    target_map = {
        "stock": Decimal(str(target.stocks)),
        "crypto": Decimal(str(target.crypto)),
        "cash": Decimal(str(target.cash)),
    }
    for bucket, target_pct in target_map.items():
        actual_pct = actual_by.get(bucket, Decimal("0"))
        rows.append(AllocationDriftRow(
            bucket=bucket,
            target_pct=target_pct,
            actual_pct=actual_pct,
            drift_pct=(actual_pct - target_pct),
        ))
    return rows


def _build_last_scan() -> LastScanBlock | None:
    persisted = read_last_scan()
    if persisted is None:
        return None

    def _color(action: str) -> str:
        if action == "placed_order":
            return "emerald"
        if action == "rejected_by_risk":
            return "rose"
        if action == "hold":
            return "amber"
        return "slate"

    decisions = [
        DecisionRow(
            symbol=d.symbol, action=d.action, reason=d.reason,
            badge_color=_color(d.action),
        )
        for d in persisted.decisions
    ]
    placed = sum(1 for d in persisted.decisions if d.action == "placed_order")
    rejected = sum(1 for d in persisted.decisions if d.action == "rejected_by_risk")
    holds = sum(1 for d in persisted.decisions if d.action == "hold")
    return LastScanBlock(
        command=persisted.command, regime=persisted.regime,
        universe_size=persisted.universe_size,
        timestamp=persisted.timestamp,
        placed=placed, rejected=rejected, holds=holds,
        decisions=decisions,
    )


def build_snapshot(
    *,
    settings: Settings,
    config: AppConfig,
    opportunities_path: Path,
    watchlist_path: Path,
    closed_db_path: Path,
) -> DashboardSnapshot:
    """One-shot: build everything the template needs."""
    errors: list[str] = []
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)

    # Regime + macro
    try:
        macro = get_macro_snapshot()
        vix = macro.vix
    except Exception as e:
        errors.append(f"macro: {e}")
        vix = None

    try:
        reading = detect_regime(market, vix=vix, vol_threshold_pct=config.regime.vol_threshold_pct)
        regime = reading.regime.value
        regime_notes = reading.notes
        vol_pct = reading.vol_annualized_pct
    except Exception as e:
        errors.append(f"regime: {e}")
        regime = "unknown"
        regime_notes = ""
        vol_pct = 0.0

    # Account + positions
    kpi, positions = _build_kpi(alpaca, errors)

    # Equity curve + today P&L + max drawdown
    curve, today_pct, max_dd = _build_equity_curve(settings, errors)
    kpi = KpiBlock(
        equity=kpi.equity, cash=kpi.cash,
        cash_pct=kpi.cash_pct, invested_pct=kpi.invested_pct,
        open_pnl=kpi.open_pnl,
        today_pnl_pct=today_pct,
        max_drawdown_pct=max_dd,
        open_position_count=kpi.open_position_count,
    )

    # Closed-trade stats
    try:
        store = ClosedTradeStore(closed_db_path)
        closed = store.all()
    except Exception as e:
        errors.append(f"closed_trades: {e}")
        closed = []
    stats = _build_stats(closed)

    orders = _build_orders(settings, errors)
    opps = _build_opportunities(opportunities_path)
    exposure = _build_exposure(positions, kpi.equity, kpi.cash)
    universe_size, universe_source = _build_universe_meta(opportunities_path, watchlist_path)

    # Tier 1 additions
    halt, headroom = _build_risk_state_and_headroom(settings, config, kpi, positions, errors)
    scheduled = _build_scheduled_jobs(errors)
    macro = _build_macro(errors)
    drift = _build_allocation_drift(config, regime, exposure)
    last_scan = _build_last_scan()

    # Automation status: simple heuristic — DOWN if equity unreachable, WARN if errors, OK otherwise
    if kpi.equity == 0 and "alpaca account/positions" in " ".join(errors):
        automation_status, automation_note = "DOWN", "Alpaca unreachable"
    elif errors:
        automation_status, automation_note = "WARN", f"{len(errors)} non-fatal error(s)"
    else:
        automation_status, automation_note = "OK", "all data sources healthy"

    return DashboardSnapshot(
        generated_at=datetime.now(timezone.utc),
        regime=regime,
        regime_notes=regime_notes,
        vix=vix,
        vol_pct=vol_pct,
        automation_status=automation_status,
        automation_note=automation_note,
        kpi=kpi,
        stats=stats,
        positions=positions,
        orders=orders,
        opportunities=opps,
        exposure=exposure,
        equity_curve=curve,
        universe_size=universe_size,
        universe_source=universe_source,
        risk_headroom=headroom,
        halt=halt,
        scheduled_jobs=scheduled,
        macro=macro,
        allocation_drift=drift,
        last_scan=last_scan,
        errors=errors,
    )
