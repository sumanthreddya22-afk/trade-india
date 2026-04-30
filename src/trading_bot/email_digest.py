"""Daily digest email builder. Sent at 18:00 ET Mon-Fri by Reporter role.
Phase 1 version. Phase 2 adds role report cards; Phase 3 adds
leaderboard summary.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from trading_bot.email_fill import Email, _fmt_money
from trading_bot.roles.base import ReportCard, HealthStatus


@dataclass
class TradeRow:
    side: str
    symbol: str
    qty: Decimal
    price: Decimal
    strategy: str
    time: dt.time
    status: str  # "open" | "closed" | "stopped"


@dataclass
class DigestContext:
    # Existing fields (keep for backward compat)
    date: dt.date
    starting_equity: Decimal
    ending_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    regime: str
    active_config_version: str
    trades: list[TradeRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    role_report_cards: list[ReportCard] = field(default_factory=list)

    # New fields for the rebuild
    equity_30d: list[Decimal] = field(default_factory=list)   # 30 daily-close equity values, oldest first
    daily_loss_cap_pct: float = 2.0
    weekly_loss_cap_pct: float = 5.0
    drawdown_pct: float = 0.0
    drawdown_cap_pct: float = 20.0
    consecutive_losing_days: int = 0
    consecutive_losing_days_cap: int = 3
    daily_loss_pct: float = 0.0
    weekly_loss_pct: float = 0.0
    vix: float | None = None
    vol_threshold_pct: float = 22.0
    positions: list[dict] = field(default_factory=list)
    closed_trades_7d: list[dict] = field(default_factory=list)
    pending_promotions: list[dict] = field(default_factory=list)
    watchlist_movers: list[dict] = field(default_factory=list)
    sentiment_scores: list[dict] = field(default_factory=list)
    schedule_audit_warnings: list[dict] = field(default_factory=list)
    daemon_blips: int = 0
    emails_sent_by_kind: dict[str, int] = field(default_factory=dict)
    git_sha: str = "unknown"
    version: str = "unknown"
    dashboard_url: str | None = None
    tomorrow_first_job: str | None = None

    # Wheel-strategy fields (Phase 5)
    wheel_open_cycles: list[dict] = field(default_factory=list)
    wheel_pnl_mtd: Decimal = Decimal("0")
    wheel_collateral_pct: float = 0.0
    wheel_win_rate: float = 0.0

    # W1.5 Decision activity — populated from DecisionStore on the day of the digest.
    decision_action_counts: dict[str, int] = field(default_factory=dict)
    decision_top_rejection_reasons: list[tuple[str, int]] = field(default_factory=list)


def build_digest_email(ctx: DigestContext) -> Email:
    if ctx.starting_equity == 0:
        pct = Decimal("0")
    else:
        pct = ((ctx.ending_equity - ctx.starting_equity) / ctx.starting_equity) * 100
    sign = "+" if pct >= 0 else ""
    subject = (
        f"Daily Digest | {ctx.date.strftime('%b %d')} | "
        f"{sign}{pct:.2f}% | {_fmt_money(ctx.ending_equity)}"
    )

    body = [f"<h2>{subject}</h2>"]

    body.append(f"<p><b>Regime:</b> {ctx.regime}<br>")
    body.append(f"<b>Active config:</b> {ctx.active_config_version}<br>")
    body.append(
        f"<b>Equity:</b> {_fmt_money(ctx.starting_equity)} &rarr; "
        f"{_fmt_money(ctx.ending_equity)} ({sign}{pct:.2f}%)<br>"
    )
    body.append(f"<b>Realized:</b> {_fmt_money(ctx.realized_pnl)}<br>")
    body.append(f"<b>Unrealized:</b> {_fmt_money(ctx.unrealized_pnl)}</p>")

    if ctx.trades:
        body.append("<h3>Today's trades</h3><table>")
        body.append(
            "<tr><th>Time</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th>"
            "<th>Strategy</th><th>Status</th></tr>"
        )
        for t in ctx.trades:
            body.append(
                f"<tr><td>{t.time.strftime('%H:%M')}</td><td>{t.side}</td>"
                f"<td>{t.symbol}</td><td>{t.qty}</td><td>{_fmt_money(t.price)}</td>"
                f"<td>{t.strategy}</td><td>{t.status}</td></tr>"
            )
        body.append("</table>")
    else:
        body.append("<p><i>No trades today (0 trades placed).</i></p>")

    if ctx.role_report_cards:
        body.append("<h3>Role Report Cards</h3><table>")
        body.append("<tr><th>Status</th><th>Role</th><th>KPI</th><th>Value</th><th>Δ vs prior</th><th>Summary</th></tr>")
        emoji = {
            HealthStatus.OK: "✅",
            HealthStatus.DEGRADED: "⚠️",
            HealthStatus.BLOCKED: "🔒",
            HealthStatus.FAIL: "❌",
        }
        for card in ctx.role_report_cards:
            delta = (
                f"{card.delta_vs_prior:+.3f}"
                if card.delta_vs_prior is not None else "—"
            )
            body.append(
                f"<tr><td>{emoji.get(card.health, '?')}</td>"
                f"<td><b>{card.role_name}</b></td>"
                f"<td>{card.kpi_name}</td>"
                f"<td>{card.kpi_value:.3f}</td>"
                f"<td>{delta}</td>"
                f"<td>{card.summary}</td></tr>"
            )
        body.append("</table>")

    if ctx.errors:
        body.append("<h3>Errors today</h3><ul>")
        for err in ctx.errors:
            body.append(f"<li>{err}</li>")
        body.append("</ul>")

    return Email(subject=subject, html_body="\n".join(body))


# ── New rebuilt daily digest (B3) ────────────────────────────────────────────

from trading_bot.email_shell import (  # noqa: E402
    render_shell, kpi_grid, kpi_card, sparkline_svg, section,
    progress_bar, severity_pill, data_table, footer,
    _BAD, _GOOD_LIGHT, _WARN, _TEXT_PRIMARY, _TEXT_SECONDARY,
)


def _render_session_review(review) -> str:
    """Render a SessionReview into three labeled bullet columns."""
    def _bullet_block(title: str, items: list[str], color: str, glyph: str) -> str:
        if not items:
            items = ["(none)"]
        bullets = "".join(
            f'<li style="margin:6px 0;color:{_TEXT_PRIMARY};font-size:13px;'
            f'line-height:1.55">{i}</li>'
            for i in items
        )
        return (
            f'<td valign="top" style="width:33%;padding:0 8px">'
            f'<div style="color:{color};font-size:11px;font-weight:700;'
            f'letter-spacing:1.4px;text-transform:uppercase;'
            f'margin-bottom:6px">{glyph} {title}</div>'
            f'<ul style="margin:0;padding-left:18px">{bullets}</ul></td>'
        )

    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%"><tr>'
        + _bullet_block("What went well", review.went_well,
                        _GOOD_LIGHT, "✓")
        + _bullet_block("What went wrong", review.went_wrong,
                        _BAD, "✗")
        + _bullet_block("Could be better", review.improvements,
                        _WARN, "→")
        + '</tr></table>'
    )


def build_daily_digest_email(ctx: DigestContext) -> Email:
    """13-section daily digest. Each section renders only when it has
    content; missing data degrades to a friendly message."""
    pct_change = (
        ((ctx.ending_equity - ctx.starting_equity) / ctx.starting_equity) * 100
        if ctx.starting_equity > 0 else Decimal("0")
    )
    sign = "+" if pct_change >= 0 else ""
    subject = (
        f"Daily Digest · {ctx.date.strftime('%b %d')} · "
        f"{sign}{pct_change:.2f}% · ${ctx.ending_equity:,.0f}"
    )

    # Status decision: bad if errors, warn if audit warnings or daemon blips, else ok.
    if ctx.errors:
        status = "bad"
    elif ctx.schedule_audit_warnings or ctx.daemon_blips:
        status = "warn"
    else:
        status = "ok"

    sections: list[str] = []

    # 1. KPI grid
    eq_spark = sparkline_svg([float(v) for v in ctx.equity_30d], width=80, height=20)
    sections.append(kpi_grid([
        kpi_card(label="Equity", value=f"${ctx.ending_equity:,.0f}",
                 delta=f"{sign}{pct_change:.2f}%",
                 delta_kind="good" if pct_change >= 0 else "bad",
                 sparkline_html=eq_spark),
        kpi_card(label="Today's P&L",
                 value=f"${(ctx.realized_pnl + ctx.unrealized_pnl):,.2f}",
                 delta_kind="good" if (ctx.realized_pnl + ctx.unrealized_pnl) >= 0 else "bad"),
        kpi_card(label="Realized", value=f"${ctx.realized_pnl:,.2f}"),
        kpi_card(label="Unrealized", value=f"${ctx.unrealized_pnl:,.2f}",
                 delta_kind="good" if ctx.unrealized_pnl >= 0 else "bad"),
    ]))

    # 2. EOD Session Review — what went well / wrong / improve.
    # Positioned high so the operator sees the synthesis before the data.
    from trading_bot.session_summary import review_session
    review = review_session(ctx)
    review_html = _render_session_review(review)
    sections.append(section(
        title="Session Review", glyph="\U0001f4cb", body=review_html,
        severity=("good" if not review.went_wrong else
                  ("warn" if len(review.went_wrong) <= 2 else "bad")),
    ))

    # 3. Equity 30d sparkline (full-width)
    if ctx.equity_30d:
        full_spark = sparkline_svg([float(v) for v in ctx.equity_30d],
                                   width=592, height=80)
        sections.append(section(
            title="Equity (last 30 days)", glyph="\U0001f4c9", body=full_spark,
        ))

    # 3. Risk gauges
    risk_html = "".join([
        progress_bar(value_pct=ctx.daily_loss_pct / ctx.daily_loss_cap_pct * 100
                     if ctx.daily_loss_cap_pct > 0 else 0,
                     color=_BAD if ctx.daily_loss_pct >= ctx.daily_loss_cap_pct else _GOOD_LIGHT,
                     label=f"Daily loss · {ctx.daily_loss_pct:.2f}% / {ctx.daily_loss_cap_pct}%"),
        progress_bar(value_pct=ctx.weekly_loss_pct / ctx.weekly_loss_cap_pct * 100
                     if ctx.weekly_loss_cap_pct > 0 else 0,
                     color=_BAD if ctx.weekly_loss_pct >= ctx.weekly_loss_cap_pct else _GOOD_LIGHT,
                     label=f"Weekly loss · {ctx.weekly_loss_pct:.2f}% / {ctx.weekly_loss_cap_pct}%"),
        progress_bar(value_pct=ctx.drawdown_pct / ctx.drawdown_cap_pct * 100
                     if ctx.drawdown_cap_pct > 0 else 0,
                     color=_BAD if ctx.drawdown_pct >= ctx.drawdown_cap_pct else _GOOD_LIGHT,
                     label=f"Drawdown · {ctx.drawdown_pct:.2f}% / {ctx.drawdown_cap_pct}%"),
        progress_bar(value_pct=ctx.consecutive_losing_days / ctx.consecutive_losing_days_cap * 100
                     if ctx.consecutive_losing_days_cap > 0 else 0,
                     color=_WARN if ctx.consecutive_losing_days > 0 else _GOOD_LIGHT,
                     label=f"Consecutive losing days · {ctx.consecutive_losing_days} / {ctx.consecutive_losing_days_cap}"),
    ])
    sections.append(section(title="Risk", glyph="\U0001f6e1️", body=risk_html))

    # 4. Regime + indicators
    regime_html = (
        f'<p style="color:{_TEXT_PRIMARY};font-size:13px;line-height:1.6">'
        f'<b>Regime:</b> {severity_pill(ctx.regime.replace("_", " "), "info")} &nbsp; '
        f'<b>VIX:</b> {ctx.vix if ctx.vix is not None else "—"} &nbsp; '
        f'<b>Vol threshold:</b> {ctx.vol_threshold_pct}%</p>'
    )
    sections.append(section(title="Regime", glyph="\U0001f321️", body=regime_html))

    # 5. Positions
    if ctx.positions:
        rows = [
            [p["symbol"], p["qty"], severity_pill(p["side"], "good" if p["side"] == "long" else "bad"),
             p["entry"], p["current"], p["today_pct"], p["total_pct"],
             p["stop"], p["distance_pct"], p.get("sentiment", "—"), p.get("sector", "—")]
            for p in ctx.positions
        ]
        sections.append(section(
            title="Positions", glyph="\U0001f4c8",
            body=data_table(
                headers=["Symbol", "Qty", "Side", "Entry", "Current",
                         "Today", "Total", "Stop", "Distance",
                         "Sentiment", "Sector"],
                rows=rows,
                right_align_cols=[1, 3, 4, 5, 6, 7, 8],
            ),
        ))
    else:
        sections.append(section(
            title="Positions", glyph="\U0001f4c8",
            body=f'<p style="color:{_TEXT_SECONDARY}">No open positions.</p>',
        ))

    # 6. Today's trades
    if ctx.trades:
        rows = [
            [t.time.strftime("%H:%M"), t.side, t.symbol, str(t.qty),
             f"${t.price:,.2f}", t.strategy, t.status]
            for t in ctx.trades
        ]
        sections.append(section(
            title="Today's Trades", glyph="\U0001f9e0",
            body=data_table(
                headers=["Time", "Side", "Symbol", "Qty", "Price", "Strategy", "Status"],
                rows=rows,
                right_align_cols=[3, 4],
            ),
        ))
    else:
        sections.append(section(
            title="Today's Trades", glyph="\U0001f9e0",
            body=f'<p style="color:{_TEXT_SECONDARY}">No trades today.</p>',
        ))

    # 6b. Decision activity (W1.5) — what the bot considered, not just what it traded.
    if ctx.decision_action_counts:
        total = sum(ctx.decision_action_counts.values())
        action_rows = [
            [action, str(count)]
            for action, count in sorted(
                ctx.decision_action_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
        ]
        body_html = (
            f'<p style="color:{_TEXT_SECONDARY};font-size:12px">'
            f'{total} decision(s) today.</p>'
            + data_table(
                headers=["Action", "Count"],
                rows=action_rows,
                right_align_cols=[1],
            )
        )
        if ctx.decision_top_rejection_reasons:
            reason_rows = [
                [reason, str(count)]
                for reason, count in ctx.decision_top_rejection_reasons
            ]
            body_html += (
                '<p style="margin-top:14px;font-weight:600">Top rejection reasons</p>'
                + data_table(
                    headers=["Reason", "Count"],
                    rows=reason_rows,
                    right_align_cols=[1],
                )
            )
        sections.append(section(
            title="Decision Activity", glyph="\U0001f5fa️",
            body=body_html,
        ))

    # 7. Closed trades (last 7d)
    if ctx.closed_trades_7d:
        rows = [
            [c["symbol"], f"{c['hold_hours']:.1f}h",
             f"${c['realized_pnl']:,.2f}", f"{c['pnl_pct']:+.2%}",
             c.get("exit_reason", "—")]
            for c in ctx.closed_trades_7d
        ]
        sections.append(section(
            title="Closed Trades (last 7d)", glyph="◆",
            body=data_table(
                headers=["Symbol", "Hold", "Realized", "Return", "Exit reason"],
                rows=rows,
                right_align_cols=[1, 2, 3],
            ),
        ))

    # 7b. Wheel cycles (Phase 5)
    if (ctx.wheel_open_cycles
            or ctx.wheel_pnl_mtd != Decimal("0")
            or ctx.wheel_collateral_pct > 0
            or ctx.wheel_win_rate > 0):
        wheel_kpis = kpi_grid([
            kpi_card(label="Open cycles", value=str(len(ctx.wheel_open_cycles))),
            kpi_card(label="Collateral", value=f"{ctx.wheel_collateral_pct:.1f}%"),
            kpi_card(label="MTD wheel P&L", value=f"${ctx.wheel_pnl_mtd}",
                     delta_kind="good" if ctx.wheel_pnl_mtd >= 0 else "bad"),
            kpi_card(label="Win rate", value=f"{ctx.wheel_win_rate * 100:.0f}%"),
        ])
        if ctx.wheel_open_cycles:
            wheel_rows = [
                [c.get("symbol", ""), c.get("phase", ""),
                 str(c.get("strike", "")), str(c.get("expiration", "")),
                 str(c.get("dte", "")),
                 (f"{c['delta']:.2f}" if isinstance(c.get("delta"), (int, float))
                  else str(c.get("delta", ""))),
                 str(c.get("iv", "")), str(c.get("credit", "")),
                 str(c.get("mark", "")), str(c.get("pnl", "")),
                 str(c.get("trigger_distance", ""))]
                for c in ctx.wheel_open_cycles
            ]
            wheel_body = wheel_kpis + data_table(
                headers=["Sym", "Phase", "Strike", "Exp", "DTE", "Δ",
                         "IV", "Credit", "Mark", "P&L", "Trigger"],
                rows=wheel_rows,
                right_align_cols=[2, 4, 5, 6, 7, 8, 9],
            )
        else:
            wheel_body = wheel_kpis + (
                f'<p style="color:{_TEXT_SECONDARY}">No open wheel cycles.</p>'
            )
        sections.append(section(
            title="Wheel Cycles", glyph="♻",
            body=wheel_body,
        ))

    # 8. Lab activity
    for promo in ctx.pending_promotions:
        params_rows = [[k, str(v)] for k, v in promo.get("params", {}).items()]
        body = (
            f'<p style="color:{_TEXT_PRIMARY};font-size:13px">'
            f'<b>Version:</b> {promo["version"]} &middot; '
            f'<b>Fitness:</b> {promo["fitness_at_promotion"]:.3f}<br>'
            f'<b>First-24h:</b> {promo["scans_since_promote"]} scans engaged · '
            f'{promo["entries_since_promote"]} entries · '
            f'{promo["near_misses_since_promote"]} near-misses</p>'
        )
        if params_rows:
            body += data_table(headers=["Param", "Value"], rows=params_rows)
        sev = "warn" if (promo["entries_since_promote"] == 0 and
                          promo["scans_since_promote"] > 0) else "info"
        sections.append(section(title="New Strategy", glyph="\U0001f9ea",
                                body=body, severity=sev))

    # 9. Watchlist movers
    if ctx.watchlist_movers:
        rows = [[m["symbol"], f"{m['pct']:+.2%}", m.get("note", "")]
                for m in ctx.watchlist_movers]
        sections.append(section(
            title="Watchlist Movers", glyph="\U0001f3af",
            body=data_table(headers=["Symbol", "Move", "Note"], rows=rows,
                            right_align_cols=[1]),
        ))

    # 10. Sentiment heatmap (compact table for now)
    if ctx.sentiment_scores:
        rows = [
            [s["symbol"], f"{s['score']:+.2f}", s.get("label", ""),
             str(s.get("articles", 0))]
            for s in ctx.sentiment_scores
        ]
        sections.append(section(
            title="Sentiment", glyph="\U0001f4ca",
            body=data_table(headers=["Symbol", "Score", "Label", "Articles"], rows=rows,
                            right_align_cols=[1, 3]),
        ))

    # 11. System health (only if anything to report)
    health_blocks = []
    if ctx.schedule_audit_warnings:
        rows = [[w["job_id"], str(w["expected"]), str(w["actual"]),
                 f"{w['ratio']:.2f}"] for w in ctx.schedule_audit_warnings]
        health_blocks.append(data_table(
            headers=["Job", "Expected", "Actual", "Ratio"],
            rows=rows, right_align_cols=[1, 2, 3],
        ))
    if ctx.daemon_blips:
        health_blocks.append(
            f'<p style="color:{_WARN};font-size:12px">'
            f'{ctx.daemon_blips} daemon blip(s) auto-recovered today.</p>'
        )
    if ctx.errors:
        health_blocks.append(
            "<ul>" + "".join(f"<li>{e}</li>" for e in ctx.errors) + "</ul>"
        )
    if ctx.emails_sent_by_kind:
        kinds = ", ".join(f"{k}: {v}" for k, v in sorted(ctx.emails_sent_by_kind.items()))
        health_blocks.append(
            f'<p style="color:{_TEXT_SECONDARY};font-size:12px">'
            f'Emails sent today: {kinds}</p>'
        )
    if health_blocks:
        sections.append(section(
            title="System Health", glyph="\U0001f6e0️",
            body="".join(health_blocks),
            severity="warn",
        ))

    # 12. Footer
    sections.append(footer(version=ctx.version, git_sha=ctx.git_sha,
                           dashboard_url=ctx.dashboard_url))

    body_html = render_shell(
        title=f"Daily Digest · {ctx.date.strftime('%b %d')}",
        status=status,
        timestamp_et=ctx.date.strftime("%a, %b %d %Y · 22:00 ET"),
        body_sections=sections,
    )
    return Email(subject=subject, html_body=body_html)
