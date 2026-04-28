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
