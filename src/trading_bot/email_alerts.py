"""Lightweight alert + status email templates.

Replaces the prior pattern where every operational email rendered the full
12-section daily digest with a near-empty context — making 92% of the
inbox volume look identical. Two focused templates:

  - ``build_status_email``: account snapshot. One-screen, no scroll.
  - ``build_alert_email``:  intel-scan / crypto-scan action notification.
                            Terse — operator only sees what just happened.

Both reuse the email_shell primitives (kpi_grid, section, render_shell)
so the visual identity matches the daily digest, but the content is
purpose-built per email type.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from trading_bot.email_fill import Email
from trading_bot.email_shell import (
    _BAD, _GOOD_LIGHT, _TEXT_PRIMARY, _TEXT_SECONDARY,
    data_table, footer, kpi_card, kpi_grid, render_shell, section,
)


# ---------------------------------------------------------------------------
# Status email — quick account snapshot for the `bot status` command.
# ---------------------------------------------------------------------------

@dataclass
class StatusContext:
    as_of: dt.datetime
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    regime: str
    open_positions: list[dict] = field(default_factory=list)
    open_order_count: int = 0
    last_heartbeat_age_minutes: float | None = None
    last_action: str | None = None
    git_sha: str = "unknown"
    version: str = "unknown"
    dashboard_url: str | None = None


def build_status_email(ctx: StatusContext) -> Email:
    """One-screen snapshot. Three sections: KPIs, positions, daemon health.

    Subject deliberately includes equity so two consecutive status emails
    are visibly different in the inbox even when nothing else changed."""
    subject = (
        f"Status · ${ctx.equity:,.0f} · {ctx.regime} · "
        f"{len(ctx.open_positions)} positions"
    )

    sections: list[str] = [
        kpi_grid([
            kpi_card(label="Equity", value=f"${ctx.equity:,.0f}"),
            kpi_card(label="Cash", value=f"${ctx.cash:,.0f}"),
            kpi_card(label="Buying Power", value=f"${ctx.buying_power:,.0f}"),
            kpi_card(label="Regime", value=ctx.regime),
        ]),
    ]

    # Open positions — only render when there are any.
    if ctx.open_positions:
        rows = [
            [str(p.get("symbol", "?")), str(p.get("qty", "")),
             f"${p.get('avg_entry_price', 0):,.4f}",
             f"${p.get('market_value', 0):,.2f}",
             f"${p.get('unrealized_pl', 0):+,.2f}"]
            for p in ctx.open_positions
        ]
        sections.append(section(
            title="Open Positions", glyph="\U0001f4c8",
            body=data_table(
                headers=["Symbol", "Qty", "Entry", "Mkt Value", "Unrealized P&L"],
                rows=rows, right_align_cols=[1, 2, 3, 4],
            ),
        ))
    else:
        sections.append(section(
            title="Open Positions", glyph="\U0001f4c8",
            body=f'<p style="color:{_TEXT_SECONDARY};margin:0">No open positions.</p>',
        ))

    # Daemon health — heartbeat age + last action + open-order count.
    health_lines: list[str] = []
    if ctx.last_heartbeat_age_minutes is not None:
        color = _GOOD_LIGHT if ctx.last_heartbeat_age_minutes < 5 else _BAD
        health_lines.append(
            f'<span style="color:{color}">Heartbeat: '
            f'{ctx.last_heartbeat_age_minutes:.1f} min ago</span>'
        )
    if ctx.last_action:
        health_lines.append(f"Last action: <code>{ctx.last_action}</code>")
    health_lines.append(f"Open orders: {ctx.open_order_count}")
    sections.append(section(
        title="Daemon", glyph="⚙️",
        body="<br>".join(health_lines),
    ))

    sections.append(footer(
        version=ctx.version, git_sha=ctx.git_sha,
        dashboard_url=ctx.dashboard_url,
    ))

    return Email(
        subject=subject,
        html_body=render_shell(
            title="Status",
            status="ok",
            timestamp_et=ctx.as_of.strftime("%a, %b %d %Y · %H:%M ET"),
            body_sections=sections,
        ),
    )


# ---------------------------------------------------------------------------
# Alert email — terse action notification from intel-scan / crypto-scan.
# ---------------------------------------------------------------------------

@dataclass
class AlertContext:
    as_of: dt.datetime
    workflow: str  # "intel-scan" | "crypto-scan" | etc.
    regime: str
    placed: list[dict] = field(default_factory=list)   # {symbol, reason, entry_order_id}
    rejected: list[dict] = field(default_factory=list)  # {symbol, reason}
    skipped_intel: list[dict] = field(default_factory=list)
    skipped_data_quality: list[dict] = field(default_factory=list)
    decision_counts: dict[str, int] = field(default_factory=dict)
    git_sha: str = "unknown"
    version: str = "unknown"
    dashboard_url: str | None = None


def build_alert_email(ctx: AlertContext) -> Email:
    """Action notification. Three sections: what happened, what was blocked,
    decision counts. Hides empty sections — operator only sees signal."""
    subject = (
        f"{ctx.workflow.title()} · {len(ctx.placed)} placed · "
        f"{len(ctx.rejected)} rejected · {ctx.regime}"
    )

    sections: list[str] = []

    # 1. Placed — green section, top of email since this is the action.
    if ctx.placed:
        rows = [
            [str(p["symbol"]), str(p.get("reason", ""))[:80],
             str(p.get("entry_order_id", ""))[:12]]
            for p in ctx.placed
        ]
        sections.append(section(
            title=f"Placed ({len(ctx.placed)})", glyph="✓",
            body=data_table(
                headers=["Symbol", "Reason", "Order ID"],
                rows=rows,
            ),
            severity="good",
        ))

    # 2. Rejected by risk — red section, what was blocked and why.
    if ctx.rejected:
        rows = [
            [str(r["symbol"]), str(r.get("reason", ""))[:100]]
            for r in ctx.rejected
        ]
        sections.append(section(
            title=f"Rejected by Risk ({len(ctx.rejected)})", glyph="✗",
            body=data_table(headers=["Symbol", "Reason"], rows=rows),
            severity="bad",
        ))

    # 3. Skipped by intel/data-quality — yellow section, surface only when
    # something interesting happened (not the routine "RSI out of band" hold).
    interesting_skips = list(ctx.skipped_intel) + list(ctx.skipped_data_quality)
    if interesting_skips:
        rows = [
            [str(s["symbol"]), str(s.get("action", ""))[:24],
             str(s.get("reason", ""))[:80]]
            for s in interesting_skips
        ]
        sections.append(section(
            title=f"Skipped ({len(interesting_skips)})", glyph="⚠",
            body=data_table(
                headers=["Symbol", "Action", "Reason"], rows=rows,
            ),
            severity="warn",
        ))

    # 4. Decision counts table — compact summary even when nothing was placed.
    if ctx.decision_counts:
        rows = [
            [action, str(count)]
            for action, count in sorted(
                ctx.decision_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
        ]
        sections.append(section(
            title="Decision Activity", glyph="\U0001f5fa️",
            body=data_table(
                headers=["Action", "Count"], rows=rows, right_align_cols=[1],
            ),
        ))

    if not sections:
        # Defensive fallback — should never trigger because the caller checks
        # placed/rejected before sending.
        sections.append(section(
            title="No Action", glyph="—",
            body=f'<p style="color:{_TEXT_SECONDARY};margin:0">'
                 f'Scan completed; no orders placed and no risk rejections.</p>',
        ))

    sections.append(footer(
        version=ctx.version, git_sha=ctx.git_sha,
        dashboard_url=ctx.dashboard_url,
    ))

    severity = "bad" if ctx.rejected else ("ok" if ctx.placed else "warn")
    return Email(
        subject=subject,
        html_body=render_shell(
            title=f"{ctx.workflow.title()} Alert",
            status=severity,
            timestamp_et=ctx.as_of.strftime("%a, %b %d %Y · %H:%M ET"),
            body_sections=sections,
        ),
    )
