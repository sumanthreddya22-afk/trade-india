"""Midday Snapshot — light intraday update at 12:00 ET. Uses the same
visual shell as the daily digest, fewer sections."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from trading_bot.email_fill import Email
from trading_bot.email_shell import (
    render_shell, kpi_grid, kpi_card, section, progress_bar, severity_pill,
    data_table, footer, _BAD, _GOOD_LIGHT, _TEXT_SECONDARY,
)


@dataclass
class SnapshotContext:
    as_of: dt.datetime
    equity: Decimal
    starting_equity: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    regime: str
    positions: list[dict] = field(default_factory=list)
    trades_today: list[dict] = field(default_factory=list)
    watchlist_signals: list[dict] = field(default_factory=list)
    daily_loss_pct: float = 0.0
    drawdown_pct: float = 0.0
    daily_loss_cap_pct: float = 2.0
    drawdown_cap_pct: float = 20.0
    version: str = "unknown"
    git_sha: str = "unknown"
    dashboard_url: str | None = None

    # Phase 5: wheel watchlist (top CSP candidates by IV-rank)
    wheel_watchlist: list[dict] = field(default_factory=list)


def build_midday_snapshot_email(ctx: SnapshotContext) -> Email:
    pct = (
        ((ctx.equity - ctx.starting_equity) / ctx.starting_equity) * 100
        if ctx.starting_equity > 0 else Decimal("0")
    )
    sign = "+" if pct >= 0 else ""
    subject = (
        f"Midday Snapshot · {ctx.as_of.strftime('%b %d')} · "
        f"{sign}{pct:.2f}% · ${ctx.equity:,.0f}"
    )

    body_sections = [
        kpi_grid([
            kpi_card(label="Equity", value=f"${ctx.equity:,.0f}",
                     delta=f"{sign}{pct:.2f}%",
                     delta_kind="good" if pct >= 0 else "bad"),
            kpi_card(label="Today's P&L",
                     value=f"${(ctx.realized_pnl_today + ctx.unrealized_pnl):,.2f}",
                     delta_kind="good" if (ctx.realized_pnl_today + ctx.unrealized_pnl) >= 0 else "bad"),
            kpi_card(label="Realized", value=f"${ctx.realized_pnl_today:,.2f}"),
            kpi_card(label="Unrealized", value=f"${ctx.unrealized_pnl:,.2f}"),
        ]),
    ]

    # Trades today (so far)
    if ctx.trades_today:
        rows = [[t["time"], t["side"], t["symbol"], str(t["qty"]),
                 f"${t['price']:,.2f}", t.get("status", "-")]
                for t in ctx.trades_today]
        body_sections.append(section(
            title="Trades So Far Today", glyph="\U0001f9e0",
            body=data_table(headers=["Time", "Side", "Symbol", "Qty", "Price", "Status"],
                            rows=rows, right_align_cols=[3, 4]),
        ))
    else:
        body_sections.append(section(
            title="Trades So Far Today", glyph="\U0001f9e0",
            body=f'<p style="color:{_TEXT_SECONDARY}">No trades yet.</p>',
        ))

    # Open positions intraday
    if ctx.positions:
        rows = [[p["symbol"], p["qty"],
                 severity_pill(p["side"], "good" if p["side"] == "long" else "bad"),
                 p["entry"], p["current"], p.get("total_pct", "—")]
                for p in ctx.positions]
        body_sections.append(section(
            title="Open Positions", glyph="\U0001f4c8",
            body=data_table(
                headers=["Symbol", "Qty", "Side", "Entry", "Current", "P&L %"],
                rows=rows, right_align_cols=[1, 3, 4, 5],
            ),
        ))

    # Watchlist signals (informational)
    if ctx.watchlist_signals:
        rows = [[s["symbol"], f"{s['distance_to_trigger_pct']:.1f}%",
                 s.get("note", "")] for s in ctx.watchlist_signals]
        body_sections.append(section(
            title="Watchlist (close to triggering)", glyph="\U0001f3af",
            body=data_table(headers=["Symbol", "Distance", "Note"], rows=rows,
                            right_align_cols=[1]),
        ))

    # Wheel watchlist (Phase 5)
    if ctx.wheel_watchlist:
        rows = [
            [w.get("symbol", ""),
             f"{w['iv_rank']:.0f}" if isinstance(w.get("iv_rank"), (int, float))
             else str(w.get("iv_rank", "")),
             f"{w['best_csp_delta']:.2f}" if isinstance(w.get("best_csp_delta"), (int, float))
             else str(w.get("best_csp_delta", "")),
             str(w.get("best_csp_strike", "")),
             f"{w.get('annualized_yield_pct', '')}%"]
            for w in ctx.wheel_watchlist
        ]
        body_sections.append(section(
            title="Wheel watchlist", glyph="♻",
            body=data_table(
                headers=["Sym", "IV-rank", "Best CSP Δ", "Strike", "Ann. yield"],
                rows=rows,
                right_align_cols=[1, 2, 3, 4],
            ),
        ))

    # Risk gauges (compact)
    risk_html = "".join([
        progress_bar(value_pct=ctx.daily_loss_pct / ctx.daily_loss_cap_pct * 100
                     if ctx.daily_loss_cap_pct > 0 else 0,
                     color=_BAD if ctx.daily_loss_pct >= ctx.daily_loss_cap_pct else _GOOD_LIGHT,
                     label=f"Daily loss · {ctx.daily_loss_pct:.2f}% / {ctx.daily_loss_cap_pct}%"),
        progress_bar(value_pct=ctx.drawdown_pct / ctx.drawdown_cap_pct * 100
                     if ctx.drawdown_cap_pct > 0 else 0,
                     color=_BAD if ctx.drawdown_pct >= ctx.drawdown_cap_pct else _GOOD_LIGHT,
                     label=f"Drawdown · {ctx.drawdown_pct:.2f}% / {ctx.drawdown_cap_pct}%"),
    ])
    body_sections.append(section(title="Risk", glyph="\U0001f6e1️", body=risk_html))

    # Freshness audit — silent green is the norm, surface only when stale.
    # The same check runs at 16:30 ET in the daily digest; midday gives the
    # operator a half-day-earlier signal so a stale cache doesn't poison the
    # afternoon scans before EOD.
    try:
        from trading_bot.freshness_audit import audit_freshness
        findings = audit_freshness()
        stale = [f for f in findings if f.severity != "ok"]
        if stale:
            rows = [
                [f.cache, f"{f.age_hours:.1f}h", f"{f.budget_hours:.1f}h", f.severity]
                for f in stale
            ]
            body_sections.append(section(
                title="Stale Data", glyph="⚠️",
                body=(
                    f'<p style="color:{_BAD};font-size:13px">'
                    f'Caches over budget — afternoon scans may run on stale data.</p>'
                    + data_table(
                        headers=["Cache", "Age", "Budget", "Severity"],
                        rows=rows, right_align_cols=[1, 2],
                    )
                ),
                severity="bad",
            ))
    except Exception:
        # Freshness audit must never block the snapshot.
        pass

    body_sections.append(footer(version=ctx.version, git_sha=ctx.git_sha,
                                dashboard_url=ctx.dashboard_url))

    return Email(
        subject=subject,
        html_body=render_shell(
            title=f"Midday Snapshot · {ctx.as_of.strftime('%b %d')}",
            status="ok" if (ctx.realized_pnl_today + ctx.unrealized_pnl) >= 0 else "warn",
            timestamp_et=ctx.as_of.strftime("%a, %b %d %Y · 12:00 ET"),
            body_sections=body_sections,
        ),
    )
