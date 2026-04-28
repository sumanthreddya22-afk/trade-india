"""HTML email report builders.

Design notes (because emails are a special hellscape):

- Inline styles only. Gmail and Outlook strip <style> blocks aggressively;
  classes are unreliable. Every visual choice lives in `style="…"`.
- Tables for layout. Flexbox/grid are uneven across email clients; nested
  tables with role="presentation" are the durable choice.
- Width capped at 640px. The shell scales down to phone widths via the
  `width:100%; max-width:640px` pattern used by every modern email
  framework.
- Dark theme by default. The bot operator reads these on a phone late at
  night; bright white panels are visually loud.
- Unicode glyphs (▲ ▼ ● ◆) instead of icon fonts or SVG — those don't
  render in many clients.

Public API kept stable:
    build_daily_report_html(...)         — basic post-scan email
    build_rich_report_html(...)          — comprehensive mid/eod email
    build_alert_email_html(events, ...)  — portfolio-watch alert
    build_naked_stops_email_html(naked)  — verify-stops alert (NEW)
    build_vip_alert_email_html(high)     — vip-scan alert (NEW)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.intelligence import IntelligenceBundle
from trading_bot.orchestrator import ScanResult
from trading_bot.portfolio_monitor import Event


# --------------------------------------------------------------------------
# Design tokens — single source of truth so all emails feel like one product.
# --------------------------------------------------------------------------

_BG_PAGE = "#0b1220"      # outer page background
_BG_CARD = "#131c30"      # card / table background
_BG_ROW_ALT = "#1a2440"   # zebra row
_BORDER = "#1f2a44"       # subtle dividers
_TEXT_PRIMARY = "#e6edf7"
_TEXT_SECONDARY = "#94a3b8"
_TEXT_MUTED = "#64748b"
_ACCENT = "#22d3ee"       # cyan
_GOOD = "#34d399"         # emerald
_BAD = "#fb7185"          # rose
_WARN = "#fbbf24"         # amber
_INFO = "#60a5fa"         # blue

_FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Inter', "
    "Helvetica, Arial, sans-serif"
)
_MONO_STACK = "'SF Mono', 'JetBrains Mono', Menlo, Consolas, monospace"


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def _fmt_money(v) -> str:
    try:
        d = Decimal(str(v))
    except Exception:
        return "—"
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


def _fmt_signed_money(v) -> str:
    try:
        d = Decimal(str(v))
    except Exception:
        return "—"
    if d > 0:
        return f"+${d:,.2f}"
    if d < 0:
        return f"-${abs(d):,.2f}"
    return "$0.00"


def _fmt_pct(v, *, signed: bool = False) -> str:
    try:
        d = Decimal(str(v))
    except Exception:
        return "—"
    if signed and d > 0:
        return f"+{d:.2f}%"
    if d == 0:
        return "0.00%"
    return f"{d:.2f}%"


def _pnl_color(v) -> str:
    try:
        d = Decimal(str(v))
    except Exception:
        return _TEXT_PRIMARY
    if d > 0:
        return _GOOD
    if d < 0:
        return _BAD
    return _TEXT_PRIMARY


# --------------------------------------------------------------------------
# Atomic UI components
# --------------------------------------------------------------------------


def _pill(text: str, kind: str = "neutral") -> str:
    """Small colored badge for severity / regime / status labels."""
    colors = {
        "good":    (_GOOD,   "rgba(52,211,153,0.12)"),
        "bad":     (_BAD,    "rgba(251,113,133,0.12)"),
        "warn":    (_WARN,   "rgba(251,191,36,0.12)"),
        "info":    (_INFO,   "rgba(96,165,250,0.12)"),
        "accent":  (_ACCENT, "rgba(34,211,238,0.12)"),
        "neutral": (_TEXT_SECONDARY, "rgba(148,163,184,0.10)"),
    }
    fg, bg = colors.get(kind, colors["neutral"])
    return (
        f"<span style=\"display:inline-block;padding:3px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-size:11px;font-weight:600;"
        f"letter-spacing:0.5px;text-transform:uppercase;font-family:{_FONT_STACK}\">"
        f"{text}</span>"
    )


def _regime_pill(regime: str) -> str:
    kind = {
        "trending_up":   "good",
        "trending_down": "bad",
        "sideways":      "warn",
        "risk_off":      "bad",
    }.get(regime, "neutral")
    return _pill(regime.replace("_", " "), kind)


def _section(title: str, body_html: str, *, accent_glyph: str = "◆") -> str:
    return (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        f"width=\"100%\" style=\"margin:24px 0 0\">"
        f"<tr><td style=\"padding:0 0 12px\">"
        f"<span style=\"color:{_ACCENT};font-size:12px;margin-right:8px\">{accent_glyph}</span>"
        f"<span style=\"color:{_TEXT_PRIMARY};font-size:14px;font-weight:600;"
        f"letter-spacing:0.4px;text-transform:uppercase;font-family:{_FONT_STACK}\">{title}</span>"
        f"</td></tr>"
        f"<tr><td>{body_html}</td></tr>"
        f"</table>"
    )


def _kpi_card(label: str, value: str, *, value_color: str = _TEXT_PRIMARY,
              sub: str | None = None) -> str:
    """Single KPI tile. Used inside _kpi_grid."""
    sub_html = (
        f"<div style=\"color:{_TEXT_MUTED};font-size:11px;margin-top:6px;"
        f"font-family:{_FONT_STACK}\">{sub}</div>"
        if sub else ""
    )
    return (
        f"<td valign=\"top\" style=\"padding:14px 16px;background:{_BG_CARD};"
        f"border:1px solid {_BORDER};border-radius:10px;width:25%\">"
        f"<div style=\"color:{_TEXT_SECONDARY};font-size:11px;letter-spacing:0.6px;"
        f"text-transform:uppercase;font-weight:600;font-family:{_FONT_STACK}\">{label}</div>"
        f"<div style=\"color:{value_color};font-size:22px;font-weight:700;margin-top:6px;"
        f"font-family:{_MONO_STACK}\">{value}</div>"
        f"{sub_html}"
        f"</td>"
    )


def _kpi_grid(cells: list[str]) -> str:
    """Lay KPI cards out in a responsive 2x2 (or 1x4) table.

    Each cell is the string from _kpi_card. We pad to 4 with blanks if fewer.
    """
    while len(cells) < 4:
        cells.append("<td style=\"width:25%\"></td>")
    spacer = "<td width=\"12\" style=\"width:12px\"></td>"
    rendered = (spacer.join(cells))
    return (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        f"width=\"100%\" style=\"margin:0\"><tr>{rendered}</tr></table>"
    )


def _empty_state(text: str) -> str:
    return (
        f"<div style=\"padding:18px;background:{_BG_CARD};border:1px dashed {_BORDER};"
        f"border-radius:10px;color:{_TEXT_MUTED};font-size:13px;text-align:center;"
        f"font-family:{_FONT_STACK}\">{text}</div>"
    )


def _data_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a styled data table. Cells are HTML — caller is responsible
    for any color/formatting on values.
    """
    if not rows:
        return _empty_state("No data.")
    th = "".join(
        f"<th align=\"left\" style=\"padding:10px 14px;color:{_TEXT_SECONDARY};"
        f"font-size:11px;font-weight:600;letter-spacing:0.6px;text-transform:uppercase;"
        f"border-bottom:1px solid {_BORDER};font-family:{_FONT_STACK}\">{h}</th>"
        for h in headers
    )
    body = []
    for i, row in enumerate(rows):
        bg = _BG_ROW_ALT if i % 2 == 0 else _BG_CARD
        tds = "".join(
            f"<td style=\"padding:10px 14px;color:{_TEXT_PRIMARY};font-size:13px;"
            f"border-bottom:1px solid {_BORDER};font-family:{_MONO_STACK}\">{c}</td>"
            for c in row
        )
        body.append(f"<tr style=\"background:{bg}\">{tds}</tr>")
    return (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        f"width=\"100%\" style=\"border-collapse:separate;border-spacing:0;"
        f"background:{_BG_CARD};border:1px solid {_BORDER};border-radius:10px;"
        f"overflow:hidden\">"
        f"<thead><tr>{th}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table>"
    )


# --------------------------------------------------------------------------
# Page shell
# --------------------------------------------------------------------------


def _shell(*, title: str, subtitle_html: str, body_html: str,
           accent: str = _ACCENT, footer_note: str | None = None) -> str:
    """Wrap content in the polished email shell.

    `subtitle_html` may include pills, dates, etc. — it's HTML, not text.
    `accent` colors the header strip.
    """
    now_str = datetime.now(timezone.utc).strftime("%a %b %d, %H:%M UTC")
    footer = footer_note or (
        f"Trading Bot · paper account · sent automatically · "
        f"<span style=\"color:{_TEXT_MUTED}\">{now_str}</span>"
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:{_BG_PAGE};">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{_BG_PAGE};">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="max-width:640px;background:{_BG_PAGE};font-family:{_FONT_STACK}">

  <!-- Header strip -->
  <tr><td style="padding:0 0 4px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td style="padding:6px 0">
          <div style="display:inline-block;width:8px;height:8px;border-radius:999px;
                      background:{accent};vertical-align:middle"></div>
          <span style="color:{_TEXT_SECONDARY};font-size:12px;font-weight:600;
                       letter-spacing:0.8px;text-transform:uppercase;margin-left:8px;
                       vertical-align:middle">Trading Bot</span>
        </td>
        <td align="right" style="color:{_TEXT_MUTED};font-size:11px;
                                  font-family:{_MONO_STACK}">{now_str}</td>
      </tr>
    </table>
  </td></tr>

  <!-- Title block -->
  <tr><td style="padding:8px 0 4px">
    <h1 style="margin:0;color:{_TEXT_PRIMARY};font-size:24px;font-weight:700;
               letter-spacing:-0.3px;font-family:{_FONT_STACK}">{title}</h1>
  </td></tr>
  <tr><td style="padding:0 0 12px;color:{_TEXT_SECONDARY};font-size:13px">
    {subtitle_html}
  </td></tr>

  <!-- Accent rule -->
  <tr><td style="padding:6px 0 0">
    <div style="height:2px;background:linear-gradient(90deg,{accent},transparent);
                border-radius:2px"></div>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:8px 0 0">{body_html}</td></tr>

  <!-- Footer -->
  <tr><td style="padding:28px 0 4px;border-top:1px solid {_BORDER};margin-top:24px">
    <div style="color:{_TEXT_MUTED};font-size:11px;text-align:center;padding-top:14px">
      {footer}
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>
"""


# --------------------------------------------------------------------------
# Position / decision tables (shared across daily + rich + alerts)
# --------------------------------------------------------------------------


def _positions_block(positions: list[Position]) -> str:
    if not positions:
        return _empty_state("No open positions.")
    rows = []
    for p in positions:
        try:
            pnl = Decimal(str(p.unrealized_pl))
            mv = Decimal(str(p.market_value))
            entry = Decimal(str(p.avg_entry_price))
        except Exception:
            pnl, mv, entry = Decimal(0), Decimal(0), Decimal(0)
        try:
            pnl_pct = (pnl / mv * Decimal("100")) if mv else Decimal(0)
        except Exception:
            pnl_pct = Decimal(0)
        glyph = "▲" if pnl > 0 else ("▼" if pnl < 0 else "●")
        pnl_html = (
            f"<span style=\"color:{_pnl_color(pnl)}\">"
            f"{glyph} {_fmt_signed_money(pnl)} "
            f"<span style=\"color:{_TEXT_MUTED};font-size:11px\">"
            f"({_fmt_pct(pnl_pct, signed=True)})</span></span>"
        )
        rows.append([
            f"<strong style=\"color:{_TEXT_PRIMARY};font-family:{_FONT_STACK}\">{p.symbol}</strong>"
            f" <span style=\"color:{_TEXT_MUTED};font-size:11px\">"
            f"{p.asset_class.replace('us_equity', 'stock')}</span>",
            f"{p.qty}",
            _fmt_money(entry),
            _fmt_money(mv),
            pnl_html,
        ])
    return _data_table(
        headers=["Symbol", "Qty", "Avg Entry", "Market Value", "Unrealized P&L"],
        rows=rows,
    )


def _decisions_block(scan: ScanResult) -> str:
    decisions = list(getattr(scan, "decisions", []) or [])
    if not decisions:
        return _empty_state("No decisions in this run.")
    action_kind = {
        "placed_order":              "good",
        "rejected_by_risk":          "bad",
        "hold":                      "neutral",
        "skipped_existing_position": "neutral",
        "skipped_no_signal":         "neutral",
    }
    rows = []
    for d in decisions:
        kind = action_kind.get(d.action, "info")
        rows.append([
            f"<strong style=\"color:{_TEXT_PRIMARY};font-family:{_FONT_STACK}\">{d.symbol}</strong>",
            _pill(d.action.replace("_", " "), kind),
            f"<span style=\"color:{_TEXT_SECONDARY};font-family:{_FONT_STACK};"
            f"font-size:12px\">{d.reason or '—'}</span>",
        ])
    return _data_table(headers=["Symbol", "Action", "Reason"], rows=rows)


# --------------------------------------------------------------------------
# Public builders
# --------------------------------------------------------------------------


def build_daily_report_html(
    *,
    account: AccountSnapshot,
    positions: list[Position],
    scan: ScanResult,
    spy_daily_change_pct: Decimal,
    regime: str,
) -> str:
    """Standard post-scan email — KPIs + positions + decisions."""
    spy_color = _pnl_color(spy_daily_change_pct)
    open_pnl = sum((Decimal(str(p.unrealized_pl)) for p in positions), Decimal(0))

    kpis = _kpi_grid([
        _kpi_card("Equity", _fmt_money(account.equity)),
        _kpi_card("Cash", _fmt_money(account.cash),
                  sub=f"{(Decimal(str(account.cash))/Decimal(str(account.equity))*100):.1f}% of equity"
                  if Decimal(str(account.equity)) > 0 else None),
        _kpi_card("Open P&L", _fmt_signed_money(open_pnl), value_color=_pnl_color(open_pnl),
                  sub=f"{len(positions)} open position{'s' if len(positions) != 1 else ''}"),
        _kpi_card("SPY Today", _fmt_pct(spy_daily_change_pct, signed=True),
                  value_color=spy_color),
    ])

    body = (
        kpis
        + _section("Open Positions", _positions_block(positions))
        + _section("Decisions This Run", _decisions_block(scan))
    )

    subtitle = (
        f"Daily snapshot · {_regime_pill(regime)} "
        f"<span style=\"color:{_TEXT_MUTED};margin:0 6px\">·</span> "
        f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_MUTED};font-size:12px\">"
        f"{scan.timestamp.isoformat(timespec='seconds')}</span>"
    )

    return _shell(title="Daily Report", subtitle_html=subtitle, body_html=body)


def build_rich_report_html(
    *,
    period: str,
    account: AccountSnapshot,
    positions: list[Position],
    scan: ScanResult,
    spy_daily_change_pct: Decimal,
    regime: str,
    intel: IntelligenceBundle,
    events: list[Event] | None = None,
    engine=None,  # state.db engine — pass to surface Phase 1-6 system status
) -> str:
    """Comprehensive mid-day / end-of-day report."""
    period_label = {"mid": "Mid-Day Report", "eod": "End-of-Day Report"}.get(
        period, f"{period.upper()} Report"
    )
    spy_color = _pnl_color(spy_daily_change_pct)
    open_pnl = sum((Decimal(str(p.unrealized_pl)) for p in positions), Decimal(0))

    kpis = _kpi_grid([
        _kpi_card("Equity", _fmt_money(account.equity)),
        _kpi_card("Cash", _fmt_money(account.cash),
                  sub=f"{(Decimal(str(account.cash))/Decimal(str(account.equity))*100):.1f}% of equity"
                  if Decimal(str(account.equity)) > 0 else None),
        _kpi_card("Open P&L", _fmt_signed_money(open_pnl), value_color=_pnl_color(open_pnl),
                  sub=f"{len(positions)} open position{'s' if len(positions) != 1 else ''}"),
        _kpi_card("SPY Today", _fmt_pct(spy_daily_change_pct, signed=True),
                  value_color=spy_color),
    ])

    # Macro snapshot
    m = intel.macro
    macro_rows = []
    if m.vix is not None:
        vix_kind = "bad" if m.vix > 28 else ("warn" if m.vix > 22 else "good")
        macro_rows.append(["VIX", f"{m.vix:.2f}", _pill(
            "elevated" if m.vix > 22 else "calm", vix_kind
        )])
    else:
        macro_rows.append(["VIX", "—", _pill("no data", "neutral")])
    if m.yield_10y_pct is not None:
        macro_rows.append(["10Y Treasury", f"{m.yield_10y_pct:.2f}%", ""])
    if m.fed_funds_pct is not None:
        macro_rows.append(["Fed Funds", f"{m.fed_funds_pct:.2f}%", ""])

    macro_html = _data_table(
        headers=["Indicator", "Value", "Status"],
        rows=macro_rows or [["—", "—", _pill("no data", "neutral")]],
    )

    body = (
        kpis
        + _section("Macro Snapshot", macro_html, accent_glyph="◈")
        + _section("Open Positions", _positions_block(positions))
        + _section("Decisions This Run", _decisions_block(scan))
    )

    # Phase 1-6 system status (strategy mode, halts, lab evolution, calibrator,
    # LLM spend, role health). Skipped when engine is unavailable so legacy
    # callers don't break.
    if engine is not None:
        try:
            body += build_system_status_section(engine)
        except Exception:
            # Never let a state.db hiccup break the email — log silently.
            pass

    # Portfolio events
    if events:
        ev_rows = []
        for e in events:
            kind = "bad" if e.severity == "alert" else "info"
            ev_rows.append([
                _pill(e.severity, kind),
                f"<span style=\"color:{_TEXT_SECONDARY};font-family:{_FONT_STACK};"
                f"font-size:12px\">{e.kind}</span>",
                f"<strong style=\"color:{_TEXT_PRIMARY};font-family:{_FONT_STACK}\">"
                f"{e.symbol or '—'}</strong>",
                f"<span style=\"color:{_TEXT_PRIMARY};font-family:{_FONT_STACK};"
                f"font-size:12px\">{e.message}</span>",
            ])
        body += _section(
            "Portfolio Events Since Last Snapshot",
            _data_table(headers=["Severity", "Kind", "Symbol", "Message"], rows=ev_rows),
            accent_glyph="●",
        )

    # Per-symbol news
    news_blocks = []
    for sym, items in intel.news_by_symbol.items():
        if not items:
            continue
        lines = "".join(
            f"<li style=\"margin:6px 0;color:{_TEXT_PRIMARY};font-size:13px;"
            f"font-family:{_FONT_STACK}\">"
            f"<a href=\"{n.url}\" style=\"color:{_ACCENT};text-decoration:none\">{n.headline}</a>"
            f" <span style=\"color:{_TEXT_MUTED};font-size:11px\">"
            f"({n.published_at.strftime('%H:%M UTC')} · {n.source})</span>"
            f"</li>"
            for n in items
        )
        news_blocks.append(
            f"<div style=\"margin-top:10px;padding:12px 16px;background:{_BG_CARD};"
            f"border:1px solid {_BORDER};border-radius:10px\">"
            f"<div style=\"color:{_ACCENT};font-size:13px;font-weight:600;"
            f"margin-bottom:4px;font-family:{_FONT_STACK}\">{sym}</div>"
            f"<ul style=\"margin:0;padding:0 0 0 18px\">{lines}</ul>"
            f"</div>"
        )
    news_body = "".join(news_blocks) if news_blocks else _empty_state("No fresh per-symbol headlines.")
    body += _section("Per-Symbol News (last 48h)", news_body, accent_glyph="✦")

    # GDELT macro news
    if intel.gdelt:
        rows = "".join(
            f"<li style=\"margin:6px 0;color:{_TEXT_PRIMARY};font-size:13px;"
            f"font-family:{_FONT_STACK}\">"
            f"<a href=\"{e.url}\" style=\"color:{_ACCENT};text-decoration:none\">{e.title}</a>"
            f" <span style=\"color:{_TEXT_MUTED};font-size:11px\">"
            f"(tone {e.sentiment:+.1f} · {e.sourcecountry})</span>"
            f"</li>"
            for e in intel.gdelt[:6]
        )
        gdelt_body = (
            f"<div style=\"padding:12px 16px;background:{_BG_CARD};"
            f"border:1px solid {_BORDER};border-radius:10px\">"
            f"<ul style=\"margin:0;padding:0 0 0 18px\">{rows}</ul></div>"
        )
        body += _section("Global Macro News (GDELT)", gdelt_body, accent_glyph="◇")

    # Insider filings
    if intel.insider:
        rows = "".join(
            f"<li style=\"margin:6px 0;color:{_TEXT_PRIMARY};font-size:13px;"
            f"font-family:{_FONT_STACK}\">{f.company} "
            f"<span style=\"color:{_TEXT_MUTED};font-size:11px\">"
            f"({f.filed_at[:10]})</span></li>"
            for f in intel.insider[:8]
        )
        ins_body = (
            f"<div style=\"padding:12px 16px;background:{_BG_CARD};"
            f"border:1px solid {_BORDER};border-radius:10px\">"
            f"<ul style=\"margin:0;padding:0 0 0 18px\">{rows}</ul></div>"
        )
        body += _section("Recent Insider Filings (Form 4)", ins_body, accent_glyph="◇")

    subtitle = (
        f"{period_label} · {_regime_pill(regime)} "
        f"<span style=\"color:{_TEXT_MUTED};margin:0 6px\">·</span> "
        f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_MUTED};font-size:12px\">"
        f"{scan.timestamp.isoformat(timespec='seconds')}</span>"
    )

    return _shell(title=period_label, subtitle_html=subtitle, body_html=body)


# --------------------------------------------------------------------------
# Phase 1-6 system-status block. Pulls from state.db via lab_data.
# --------------------------------------------------------------------------


def _strategy_mode_block(view) -> str:
    if view is None:
        return _empty_state("Strategy mode not yet bootstrapped.")
    color = _GOOD if not view.is_fallback else _WARN
    bg = "rgba(52,211,153,0.10)" if not view.is_fallback else "rgba(251,191,36,0.10)"
    set_at_str = view.set_at.strftime("%Y-%m-%d %H:%M UTC")
    days = view.days_in_state
    days_str = "today" if days == 0 else (f"{days} day" if days == 1 else f"{days} days")
    reason_html = (
        f"<div style=\"color:{_TEXT_MUTED};font-size:12px;margin-top:6px;"
        f"font-family:{_FONT_STACK}\">{view.reason}</div>"
        if view.reason else ""
    )
    return (
        f"<div style=\"padding:16px 20px;background:{bg};border:1px solid {color};"
        f"border-radius:10px\">"
        f"<div style=\"display:inline-block;color:{color};font-size:18px;font-weight:700;"
        f"letter-spacing:1px;font-family:{_FONT_STACK}\">{view.label}</div>"
        f"<div style=\"color:{_TEXT_SECONDARY};font-size:12px;margin-top:4px;"
        f"font-family:{_FONT_STACK}\">"
        f"in this state for {days_str} (since {set_at_str}, set by {view.set_by})</div>"
        f"{reason_html}"
        f"</div>"
    )


def _halts_block(halts: list) -> str:
    """Return body HTML or empty string. build_system_status_section adds the section wrapper."""
    if not halts:
        return ""
    rows = []
    for h in halts:
        kind_pill = _pill(h.kind, "bad")
        until_str = h.halted_until.strftime("%Y-%m-%d %H:%M UTC")
        hrs = f"{h.hours_remaining:.1f}h"
        rows.append([kind_pill, until_str, hrs, (h.reason or "")[:80]])
    return _data_table(
        headers=["Kind", "Until", "Remaining", "Reason"], rows=rows
    )


def _lab_evolution_block(view) -> str:
    if view.last_run_started_at is None and not view.top_leaderboard:
        return _empty_state("Lab has not produced any leaderboard rows yet.")
    summary_kpis = []
    if view.last_run_started_at is not None:
        when = view.last_run_started_at.strftime("%b %d %H:%M UTC")
        summary_kpis.append(
            _kpi_card(
                "Last Search",
                f"{view.last_run_n_trials} trials",
                sub=f"{view.last_run_template} · {when}",
            )
        )
        if view.last_run_best_fitness is not None:
            summary_kpis.append(
                _kpi_card(
                    "Best Fitness",
                    f"{view.last_run_best_fitness:.2f}",
                    sub="auto-promoted" if view.last_run_promoted else "no promotion",
                    value_color=_GOOD if view.last_run_promoted else _TEXT_PRIMARY,
                )
            )
    rows = []
    for r in view.top_leaderboard:
        alpha = r["alpha_vs_spy_x"]
        alpha_html = (
            f"<span style=\"color:{_pnl_color(alpha - 1)}\">{alpha:.2f}x</span>"
        )
        rows.append([
            r["template"],
            alpha_html,
            f"{r['sortino']:.2f}",
            f"{r['max_dd_pct']:.1f}%",
            r["folds"],
            f"{r['fitness_score']:.2f}",
        ])
    table = _data_table(
        headers=["Template", "Alpha vs SPY", "Sortino", "Max DD", "Folds", "Fitness"],
        rows=rows or [["—", "—", "—", "—", "—", "—"]],
    )
    return (_kpi_grid(summary_kpis) if summary_kpis else "") + "<div style=\"height:12px\"></div>" + table


def _calibrator_block(view) -> str:
    if view.latest_at is None:
        return _empty_state("Calibrator has not run yet.")
    sev_kind = {
        "ok": "good",
        "warning": "warn",
        "high": "bad",
        "insufficient_data": "neutral",
        "never_run": "neutral",
    }.get(view.latest_severity, "neutral")
    corr_str = f"{view.latest_corr:.3f}" if view.latest_corr is not None else "—"
    when = view.latest_at.strftime("%b %d %H:%M UTC")
    return (
        f"<div style=\"padding:14px 16px;background:{_BG_CARD};border:1px solid {_BORDER};"
        f"border-radius:10px\">"
        f"<div style=\"font-family:{_FONT_STACK}\">"
        f"<span style=\"color:{_TEXT_PRIMARY};font-size:24px;font-weight:700;"
        f"font-family:{_MONO_STACK}\">{corr_str}</span>"
        f"<span style=\"margin-left:12px\">{_pill(view.latest_severity, sev_kind)}</span>"
        f"</div>"
        f"<div style=\"color:{_TEXT_MUTED};font-size:12px;margin-top:6px;"
        f"font-family:{_FONT_STACK}\">"
        f"Spearman corr · {view.latest_n} trade pairs · last run {when}"
        f"</div>"
        f"</div>"
    )


def _llm_spend_block(view) -> str:
    if view.n_calls_mtd == 0:
        return _empty_state("No Anthropic API calls this month.")
    pct = view.pct_used
    bar_color = _GOOD if pct < 70 else (_WARN if pct < 90 else _BAD)
    bar_w = min(100, max(0, pct))
    bar_html = (
        f"<div style=\"height:8px;background:{_BG_ROW_ALT};border-radius:999px;"
        f"overflow:hidden;margin:8px 0\">"
        f"<div style=\"width:{bar_w}%;height:100%;background:{bar_color}\"></div>"
        f"</div>"
    )
    return (
        f"<div style=\"padding:14px 16px;background:{_BG_CARD};border:1px solid {_BORDER};"
        f"border-radius:10px\">"
        f"<div style=\"font-family:{_FONT_STACK}\">"
        f"<span style=\"color:{_TEXT_PRIMARY};font-size:22px;font-weight:700;"
        f"font-family:{_MONO_STACK}\">${view.month_to_date_usd:.2f}</span>"
        f"<span style=\"color:{_TEXT_MUTED};font-size:13px;margin-left:8px\">"
        f"of ${view.monthly_cap_usd:.0f} cap ({pct:.1f}%)</span>"
        f"</div>"
        f"{bar_html}"
        f"<div style=\"color:{_TEXT_MUTED};font-size:12px;font-family:{_FONT_STACK}\">"
        f"{view.n_calls_mtd} calls MTD · "
        f"top model: {view.most_used_model or 'n/a'}"
        f"</div>"
        f"</div>"
    )


def _role_health_block(rows: list) -> str:
    if not rows:
        return _empty_state("No role runs in last 30d.")
    body_rows = []
    for r in rows:
        rate = r.success_rate_pct
        kind = "good" if rate >= 95 else ("warn" if rate >= 70 else "bad")
        last = r.last_run_at.strftime("%b %d %H:%M") if r.last_run_at else "—"
        body_rows.append([
            r.role_name,
            f"{r.runs_today}",
            f"{r.runs_30d}",
            _pill(f"{rate:.0f}%", kind),
            last,
            r.last_status,
        ])
    return _data_table(
        headers=["Role", "Today", "30d", "Success", "Last Run", "Last Status"],
        rows=body_rows,
    )


def build_system_status_section(engine) -> str:
    """Render the full Phase 1-6 system status block. Returns HTML.

    Pulls from state.db via lab_data. Sections that have no data render an
    empty-state placeholder so the layout is stable from day one.
    """
    from sqlalchemy.orm import Session

    from trading_bot import lab_data

    with Session(engine) as session:
        mode = lab_data.strategy_mode(session)
        halts = lab_data.active_halts(session)
        evolution = lab_data.lab_evolution(session)
        cal = lab_data.calibrator(session)
        spend = lab_data.llm_spend(session)
        roles = lab_data.role_health(session)

    parts = [
        _section("Strategy Mode", _strategy_mode_block(mode), accent_glyph="◆"),
    ]
    halts_html = _halts_block(halts)
    if halts_html:
        parts.append(_section("Active Halts", halts_html, accent_glyph="⚠"))
    parts.extend([
        _section("Lab Evolution", _lab_evolution_block(evolution), accent_glyph="⚗"),
        _section("Calibrator (backtest vs paper drift)", _calibrator_block(cal), accent_glyph="◎"),
        _section("LLM Spend (Anthropic)", _llm_spend_block(spend), accent_glyph="✦"),
        _section("Role Health (last 30d)", _role_health_block(roles), accent_glyph="◍"),
    ])
    return "".join(parts)


# --------------------------------------------------------------------------
# Alert emails
# --------------------------------------------------------------------------


def build_alert_email_html(events: list[Event], account_equity: str) -> str:
    """Portfolio-watch material-events alert."""
    alert_count = sum(1 for e in events if e.severity == "alert")
    info_count = len(events) - alert_count
    accent = _BAD if alert_count > 0 else _ACCENT

    kpis = _kpi_grid([
        _kpi_card("Equity", _fmt_money(account_equity)),
        _kpi_card("Alerts", str(alert_count),
                  value_color=_BAD if alert_count > 0 else _TEXT_PRIMARY),
        _kpi_card("Info Events", str(info_count), value_color=_INFO),
        _kpi_card("Total", str(len(events))),
    ])

    rows = []
    for e in events:
        kind = "bad" if e.severity == "alert" else "info"
        rows.append([
            _pill(e.severity, kind),
            f"<span style=\"color:{_TEXT_SECONDARY};font-family:{_FONT_STACK};"
            f"font-size:12px\">{e.kind}</span>",
            f"<strong style=\"color:{_TEXT_PRIMARY};font-family:{_FONT_STACK}\">"
            f"{e.symbol or '—'}</strong>",
            f"<span style=\"color:{_TEXT_PRIMARY};font-family:{_FONT_STACK};"
            f"font-size:12px\">{e.message}</span>",
        ])
    body = (
        kpis
        + _section(
            "Material Events",
            _data_table(headers=["Severity", "Kind", "Symbol", "Message"], rows=rows),
        )
    )

    subtitle = (
        f"{_pill('portfolio alert', 'bad' if alert_count else 'info')} "
        f"<span style=\"color:{_TEXT_MUTED};margin-left:8px\">"
        f"{len(events)} event{'s' if len(events) != 1 else ''}</span>"
    )
    return _shell(
        title="Portfolio Alert",
        subtitle_html=subtitle,
        body_html=body,
        accent=accent,
    )


def build_naked_stops_email_html(
    naked: list[tuple[str, str, str]],
    *,
    total_positions: int | None = None,
) -> str:
    """Verify-stops sweep alert. Each `naked` entry is (symbol, qty, side)."""
    rows = []
    for sym, qty, side in naked:
        side_clean = side.replace("PositionSide.", "").lower()
        side_kind = "good" if side_clean == "long" else "bad"
        rows.append([
            f"<strong style=\"color:{_BAD};font-family:{_FONT_STACK}\">{sym}</strong>",
            f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{qty}</span>",
            _pill(side_clean, side_kind),
        ])

    kpis = _kpi_grid([
        _kpi_card("Naked Positions", str(len(naked)), value_color=_BAD),
        _kpi_card("Total Open",
                  str(total_positions) if total_positions is not None else "—"),
        _kpi_card("Action", "manual",
                  value_color=_WARN, sub="Replace stops in Alpaca UI"),
        _kpi_card("Severity", "high", value_color=_BAD),
    ])
    intro = (
        f"<div style=\"padding:14px 16px;background:rgba(251,113,133,0.06);"
        f"border:1px solid rgba(251,113,133,0.25);border-radius:10px;"
        f"color:{_TEXT_PRIMARY};font-size:13px;line-height:1.5;font-family:{_FONT_STACK};"
        f"margin-bottom:12px\">"
        f"<strong style=\"color:{_BAD}\">⚠ {len(naked)} position"
        f"{'s have' if len(naked) != 1 else ' has'} no live stop order.</strong> "
        f"Bracket legs can detach on partial fills (Risk #8). "
        f"Crypto stops require <code style=\"color:{_ACCENT}\">stop_limit</code> "
        f"order type — Alpaca rejects plain stops on crypto. Replace stops manually "
        f"in the Alpaca UI or via API."
        f"</div>"
    )
    body = (
        intro
        + kpis
        + _section(
            "Unprotected Positions",
            _data_table(headers=["Symbol", "Quantity", "Side"], rows=rows),
            accent_glyph="⚠",
        )
    )
    subtitle = (
        f"{_pill('naked positions', 'bad')} "
        f"<span style=\"color:{_TEXT_SECONDARY};margin-left:8px\">"
        f"{len(naked)} unprotected · review immediately</span>"
    )
    return _shell(
        title="Naked Position Alert",
        subtitle_html=subtitle,
        body_html=body,
        accent=_BAD,
    )


@dataclass(frozen=True)
class _VipPostLike:
    """Minimal protocol the VIP-tweet builder cares about."""
    severity: str
    handle: str
    platform: str
    text: str
    url: str
    severity_reason: str


def build_vip_alert_email_html(high_posts: Iterable) -> str:
    """VIP-tweet HIGH-severity alert. `high_posts` items must expose
    `severity`, `handle`, `platform`, `text`, `url`, `severity_reason`.
    """
    posts = list(high_posts)
    cards = []
    for p in posts:
        cards.append(
            f"<div style=\"margin-top:10px;padding:14px 16px;background:{_BG_CARD};"
            f"border:1px solid {_BORDER};border-left:3px solid {_BAD};"
            f"border-radius:10px\">"
            f"<div style=\"margin-bottom:6px\">{_pill(p.severity, 'bad')} "
            f"<span style=\"color:{_TEXT_PRIMARY};font-weight:600;margin-left:8px;"
            f"font-family:{_FONT_STACK}\">{p.handle}</span> "
            f"<span style=\"color:{_TEXT_MUTED};font-size:11px;margin-left:6px;"
            f"font-family:{_FONT_STACK}\">{p.platform}</span></div>"
            f"<div style=\"color:{_TEXT_PRIMARY};font-size:13px;line-height:1.5;"
            f"font-family:{_FONT_STACK};margin-bottom:8px\">{p.text[:500]}</div>"
            f"<div style=\"color:{_TEXT_MUTED};font-size:11px;margin-bottom:6px;"
            f"font-family:{_FONT_STACK}\"><em>why high: {p.severity_reason}</em></div>"
            f"<a href=\"{p.url}\" style=\"color:{_ACCENT};font-size:12px;"
            f"text-decoration:none;font-family:{_MONO_STACK}\">{p.url}</a>"
            f"</div>"
        )

    intro = (
        f"<div style=\"padding:14px 16px;background:rgba(251,191,36,0.06);"
        f"border:1px solid rgba(251,191,36,0.25);border-radius:10px;"
        f"color:{_TEXT_PRIMARY};font-size:13px;line-height:1.5;font-family:{_FONT_STACK};"
        f"margin-bottom:12px\">"
        f"<strong style=\"color:{_WARN}\">Bot is alert-only — no trades placed.</strong> "
        f"Manual judgment required."
        f"</div>"
    )
    body = intro + "".join(cards)
    subtitle = (
        f"{_pill('VIP tweet alert', 'bad')} "
        f"<span style=\"color:{_TEXT_SECONDARY};margin-left:8px\">"
        f"{len(posts)} high-severity post{'s' if len(posts) != 1 else ''}</span>"
    )
    return _shell(
        title="VIP Tweet Alert",
        subtitle_html=subtitle,
        body_html=body,
        accent=_BAD,
    )
