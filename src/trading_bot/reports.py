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

Visual primitives (pills, sections, data tables, KPI cards, etc.) live in
`email_shell.py` and are imported here. This file contains only the
builder functions that compose those primitives into full emails.

Public API:
    build_open_positions_email_html(actions)  — verify-stops auto-protect summary
    open_positions_email_subject(actions)     — subject line for the above
    build_vip_alert_email_html(high)     — vip-scan alert
    build_system_status_section(engine)  — Phase 1-6 system status block (used by digest)

Removed in Task 10 (B3):
    build_daily_report_html(...)         — replaced by build_daily_digest_email
    build_rich_report_html(...)          — replaced by build_daily_digest_email

Removed in Task 14:
    build_alert_email_html(...)          — replaced by alerts.py _build_alert_email_html
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from trading_bot.shared.alpaca_client import AccountSnapshot, Position
from trading_bot.orchestrator import ScanResult
from trading_bot.email_shell import (
    # color tokens — single source of truth
    _BG_CARD,
    _BG_OUTER as _BG_PAGE,
    _BORDER,
    _TEXT_PRIMARY,
    _TEXT_SECONDARY,
    _TEXT_MUTED,
    _ACCENT,
    _ACCENT_BRIGHT,
    _GOOD,
    _GOOD_LIGHT,
    _BAD,
    _WARN,
    _INFO,
    _FONT_STACK,
    _MONO_STACK,
    # visual primitives
    severity_pill,
    section,
    data_table,
    kpi_card,
    kpi_grid,
    empty_state,
)

# Additional color tokens not exported from email_shell
_BG_PAGE_GRAD_TOP = "#070b15"
_BG_ROW_ALT = "#131c30"    # subtle zebra stripe
_PURPLE = "#a78bfa"        # violet-400 gradient stop
_CARD_RADIUS = "16px"      # dashboard .card border-radius


# --------------------------------------------------------------------------
# Formatting helpers (unique to reports — not in email_shell)
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


def _regime_pill(regime: str) -> str:
    kind = {
        "trending_up":   "good",
        "trending_down": "bad",
        "sideways":      "warn",
        "risk_off":      "bad",
    }.get(regime, "neutral")
    return severity_pill(regime.replace("_", " "), kind)


# --------------------------------------------------------------------------
# Page shell — TB-logo style, distinct from email_shell.render_shell
# --------------------------------------------------------------------------


def _shell(*, title: str, subtitle_html: str, body_html: str,
           accent: str = _ACCENT, footer_note: str | None = None) -> str:
    """Wrap content in the polished email shell with TB badge header.

    `subtitle_html` may include pills, dates, etc. — it's HTML, not text.
    `accent` colors the header strip.
    """
    now_str = datetime.now(timezone.utc).strftime("%a %b %d, %H:%M UTC")
    footer_content = footer_note or (
        f"Trading Bot · paper account · sent automatically · "
        f"<span style=\"color:{_TEXT_MUTED}\">{now_str}</span>"
    )
    # Match the dashboard sidebar's "TB" badge — a small gradient logo block.
    logo_block = (
        f"<div style=\"display:inline-block;width:36px;height:36px;border-radius:10px;"
        f"background:linear-gradient(135deg,{_ACCENT_BRIGHT} 0%,{_GOOD} 100%);"
        f"text-align:center;line-height:36px;color:#0a0f1c;font-weight:800;"
        f"font-size:14px;letter-spacing:0.5px;vertical-align:middle;font-family:{_FONT_STACK}\">"
        f"TB</div>"
    )
    title_block = (
        f"<span style=\"display:inline-block;margin-left:10px;vertical-align:middle\">"
        f"<div style=\"color:{_TEXT_PRIMARY};font-size:14px;font-weight:600;line-height:1.1;"
        f"font-family:{_FONT_STACK}\">Trading Bot</div>"
        f"<div style=\"color:{_TEXT_MUTED};font-size:11px;margin-top:2px;"
        f"font-family:{_FONT_STACK}\">Paper command</div>"
        f"</span>"
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:{_BG_PAGE};">
<!-- Outer page: matches dashboard linear-gradient(180deg,#070b15 0%,#0a0f1c 100%) -->
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{_BG_PAGE_GRAD_TOP};">
<tr><td align="center" style="padding:32px 12px;
       background:linear-gradient(180deg,{_BG_PAGE_GRAD_TOP} 0%,{_BG_PAGE} 100%)">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="max-width:680px;font-family:{_FONT_STACK}">

  <!-- Header strip: TB badge + title + timestamp on the right -->
  <tr><td style="padding:0 4px 18px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td style="vertical-align:middle">
          {logo_block}{title_block}
        </td>
        <td align="right" style="vertical-align:middle;color:{_TEXT_MUTED};font-size:11px;
                                  font-family:{_MONO_STACK}">
          <span style="display:inline-block;width:8px;height:8px;border-radius:999px;
                       background:{_GOOD};box-shadow:0 0 12px {_GOOD};vertical-align:middle;
                       margin-right:6px"></span>
          {now_str}
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Title card: matches dashboard's _header.html section -->
  <tr><td style="padding:0;background:{_BG_CARD};border:1px solid {_BORDER};
                  border-radius:{_CARD_RADIUS}">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td style="padding:22px 24px 8px">
        <h1 style="margin:0;color:{_TEXT_PRIMARY};font-size:28px;font-weight:700;
                   letter-spacing:-0.5px;line-height:1.15;font-family:{_FONT_STACK}">{title}</h1>
      </td></tr>
      <tr><td style="padding:0 24px 22px;color:{_TEXT_SECONDARY};font-size:13px;
                      font-family:{_FONT_STACK}">
        {subtitle_html}
      </td></tr>
    </table>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:0">{body_html}</td></tr>

  <!-- Footer -->
  <tr><td style="padding:32px 4px 4px">
    <div style="border-top:1px solid {_BORDER};padding-top:18px;
                color:{_TEXT_MUTED};font-size:11px;text-align:center;
                font-family:{_FONT_STACK}">
      {footer_content}
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
        return empty_state("No open positions.")
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
    return data_table(
        headers=["Symbol", "Qty", "Avg Entry", "Market Value", "Unrealized P&L"],
        rows=rows,
    )


def _decisions_block(scan: ScanResult) -> str:
    decisions = list(getattr(scan, "decisions", []) or [])
    if not decisions:
        return empty_state("No decisions in this run.")
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
            severity_pill(d.action.replace("_", " "), kind),
            f"<span style=\"color:{_TEXT_SECONDARY};font-family:{_FONT_STACK};"
            f"font-size:12px\">{d.reason or '—'}</span>",
        ])
    return data_table(headers=["Symbol", "Action", "Reason"], rows=rows)


# --------------------------------------------------------------------------
# Phase 1-6 system-status block. Pulls from state.db via lab_data.
# --------------------------------------------------------------------------


def _strategy_mode_block(view) -> str:
    """Mirrors dashboard `_strategy_mode.html` — large bold mode word with
    matching emerald (active) or amber (fallback) tone on a tinted card."""
    if view is None:
        return empty_state("Strategy mode not yet bootstrapped.")
    if view.is_fallback:
        color = _WARN
        sub_color = "#fde68a"  # amber-200
        bg_tint = "rgba(251,191,36,0.08)"
        border_tint = "rgba(251,191,36,0.35)"
        sub_text = "hold-SPY mode"
    else:
        color = _GOOD_LIGHT
        sub_color = "#a7f3d0"  # emerald-200
        bg_tint = "rgba(16,185,129,0.08)"
        border_tint = "rgba(16,185,129,0.35)"
        sub_text = f"trading {view.set_by}"
    set_at_str = view.set_at.strftime("%Y-%m-%d %H:%M UTC")
    days = view.days_in_state
    days_str = "Set today" if days == 0 else (f"{days} day in state" if days == 1 else f"{days} days in state")
    reason_html = (
        f"<div style=\"color:{_TEXT_SECONDARY};font-size:12px;margin-top:8px;"
        f"font-family:{_FONT_STACK}\">{view.reason}</div>"
        if view.reason else ""
    )
    return (
        f"<div style=\"padding:20px 24px;background:{bg_tint};border:1px solid {border_tint};"
        f"border-radius:{_CARD_RADIUS}\">"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\">"
        f"<tr><td style=\"vertical-align:baseline\">"
        f"<span style=\"color:{color};font-size:28px;font-weight:700;letter-spacing:2px;"
        f"font-family:{_FONT_STACK}\">{view.label}</span>"
        f"</td><td style=\"vertical-align:baseline;padding-left:14px\">"
        f"<span style=\"color:{sub_color};font-size:12px;"
        f"font-family:{_FONT_STACK}\">{sub_text}</span>"
        f"</td></tr></table>"
        f"<div style=\"color:{_TEXT_MUTED};font-size:12px;margin-top:8px;"
        f"font-family:{_FONT_STACK}\">"
        f"{days_str} · {set_at_str}</div>"
        f"{reason_html}"
        f"</div>"
    )


def _halts_block(halts: list) -> str:
    """Return body HTML or empty string. build_system_status_section adds the section wrapper."""
    if not halts:
        return ""
    rows = []
    for h in halts:
        kind_pill = severity_pill(h.kind, "bad")
        until_str = h.halted_until.strftime("%Y-%m-%d %H:%M UTC")
        hrs = f"{h.hours_remaining:.1f}h"
        rows.append([kind_pill, until_str, hrs, (h.reason or "")[:80]])
    return data_table(
        headers=["Kind", "Until", "Remaining", "Reason"], rows=rows
    )


def _lab_evolution_block(view) -> str:
    if view.last_run_started_at is None and not view.top_leaderboard:
        return empty_state("Lab has not produced any leaderboard rows yet.")
    summary_kpis = []
    if view.last_run_started_at is not None:
        when = view.last_run_started_at.strftime("%b %d %H:%M UTC")
        summary_kpis.append(
            kpi_card(
                label="Last Search",
                value=f"{view.last_run_n_trials} trials",
                sub=f"{view.last_run_template} · {when}",
            )
        )
        if view.last_run_best_fitness is not None:
            summary_kpis.append(
                kpi_card(
                    label="Best Fitness",
                    value=f"{view.last_run_best_fitness:.2f}",
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
    tbl = data_table(
        headers=["Template", "Alpha vs SPY", "Sortino", "Max DD", "Folds", "Fitness"],
        rows=rows or [["—", "—", "—", "—", "—", "—"]],
    )
    return (kpi_grid(summary_kpis) if summary_kpis else "") + "<div style=\"height:12px\"></div>" + tbl


def _calibrator_block(view) -> str:
    if view.latest_at is None:
        return empty_state("Calibrator has not run yet.")
    sev_kind = {
        "ok": "good",
        "warning": "warn",
        "high": "bad",
        "insufficient_data": "neutral",
        "never_run": "neutral",
    }.get(view.latest_severity, "neutral")
    corr_str = f"{view.latest_corr:.3f}" if view.latest_corr is not None else "—"
    when = view.latest_at.strftime("%b %d %H:%M UTC")
    sev_color = {
        "ok": _GOOD_LIGHT,
        "warning": _WARN,
        "high": _BAD,
    }.get(view.latest_severity, _TEXT_SECONDARY)
    return (
        f"<div style=\"padding:18px 22px;background:{_BG_CARD};border:1px solid {_BORDER};"
        f"border-radius:{_CARD_RADIUS}\">"
        f"<div style=\"font-family:{_FONT_STACK}\">"
        f"<span style=\"color:{sev_color};font-size:32px;font-weight:700;line-height:1.1;"
        f"letter-spacing:-0.02em;font-family:{_MONO_STACK}\">{corr_str}</span>"
        f"<span style=\"margin-left:14px;vertical-align:middle\">"
        f"{severity_pill(view.latest_severity.replace('_', ' '), sev_kind)}</span>"
        f"</div>"
        f"<div style=\"color:{_TEXT_MUTED};font-size:12px;margin-top:8px;"
        f"font-family:{_FONT_STACK}\">"
        f"Spearman corr · {view.latest_n} trade pair{'' if view.latest_n == 1 else 's'} · last run {when}"
        f"</div>"
        f"</div>"
    )


def _llm_spend_block(view) -> str:
    if view.n_calls_mtd == 0:
        return empty_state("No Anthropic API calls this month.")
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
        f"<div style=\"padding:18px 22px;background:{_BG_CARD};border:1px solid {_BORDER};"
        f"border-radius:{_CARD_RADIUS}\">"
        f"<div style=\"font-family:{_FONT_STACK}\">"
        f"<span style=\"color:{_TEXT_PRIMARY};font-size:32px;font-weight:700;line-height:1.1;"
        f"letter-spacing:-0.02em;font-family:{_MONO_STACK}\">${view.month_to_date_usd:.2f}</span>"
        f"<span style=\"color:{_TEXT_MUTED};font-size:13px;margin-left:10px\">"
        f"of ${view.monthly_cap_usd:.0f} cap</span>"
        f"</div>"
        f"{bar_html}"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
        f"width=\"100%\" style=\"margin-top:6px\"><tr>"
        f"<td style=\"color:{_TEXT_MUTED};font-size:12px;font-family:{_FONT_STACK}\">"
        f"{view.n_calls_mtd} call{'' if view.n_calls_mtd == 1 else 's'} this month</td>"
        f"<td align=\"right\" style=\"color:{_TEXT_MUTED};font-size:12px;font-family:{_MONO_STACK}\">"
        f"{view.most_used_model or 'n/a'}</td>"
        f"</tr></table>"
        f"</div>"
    )


def _role_health_block(rows: list) -> str:
    if not rows:
        return empty_state("No role runs in last 30d.")
    body_rows = []
    for r in rows:
        rate = r.success_rate_pct
        kind = "good" if rate >= 95 else ("warn" if rate >= 70 else "bad")
        last = r.last_run_at.strftime("%b %d %H:%M") if r.last_run_at else "—"
        body_rows.append([
            r.role_name,
            f"{r.runs_today}",
            f"{r.runs_30d}",
            severity_pill(f"{rate:.0f}%", kind),
            last,
            r.last_status,
        ])
    return data_table(
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
        section(title="Strategy Mode", glyph="◆", body=_strategy_mode_block(mode)),
    ]
    halts_html = _halts_block(halts)
    if halts_html:
        parts.append(section(title="Active Halts", glyph="⚠", body=halts_html, severity="bad"))
    parts.extend([
        section(title="Lab Evolution", glyph="⚗", body=_lab_evolution_block(evolution)),
        section(title="Calibrator (backtest vs paper drift)", glyph="◎", body=_calibrator_block(cal)),
        section(title="LLM Spend (Anthropic)", glyph="✦", body=_llm_spend_block(spend)),
        section(title="Role Health (last 30d)", glyph="◍", body=_role_health_block(roles)),
    ])
    return "".join(parts)


def open_positions_email_subject(actions) -> str:
    """Subject line for the verify-stops auto-protect summary.

    `Open Positions — N actioned`               (clean run)
    `Open Positions — N actioned, M need attention`   (any failed/deferred)
    """
    actioned = sum(
        1 for a in actions if a.outcome in ("stop_placed", "flattened")
    )
    attention = sum(
        1 for a in actions if a.outcome in ("failed", "deferred_off_hours")
    )
    if attention:
        return f"Open Positions — {actioned} actioned, {attention} need attention"
    return f"Open Positions — {actioned} actioned"


def build_open_positions_email_html(
    actions,
    *,
    total_positions: int | None = None,
) -> str:
    """Verify-stops auto-protect summary. Renders one section per outcome
    bucket; sections with no rows are omitted."""
    protected = [a for a in actions if a.outcome == "stop_placed"]
    closed = [a for a in actions if a.outcome == "flattened"]
    failed = [a for a in actions if a.outcome == "failed"]
    deferred = [a for a in actions if a.outcome == "deferred_off_hours"]

    kpis = kpi_grid([
        kpi_card(label="Total Open",
                 value=str(total_positions) if total_positions is not None else "—"),
        kpi_card(label="Stops Placed", value=str(len(protected)), value_color=_GOOD),
        kpi_card(label="Closed", value=str(len(closed)),
                 value_color=_BAD if closed else _TEXT_PRIMARY),
        kpi_card(label="Need Attention", value=str(len(failed) + len(deferred)),
                 value_color=_WARN if (failed or deferred) else _TEXT_PRIMARY),
    ])

    body_parts: list[str] = [kpis]

    if protected:
        rows = []
        for a in protected:
            distance_pct = (
                (a.current_price - a.stop_price) / a.current_price * 100.0
                if a.current_price else 0.0
            )
            rows.append([
                f"<strong style=\"color:{_GOOD_LIGHT};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
                f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
                severity_pill(a.position_side.value, "good" if a.position_side.value == "buy" else "bad"),
                f"<span style=\"font-family:{_MONO_STACK}\">${a.current_price:,.2f}</span>",
                f"<span style=\"font-family:{_MONO_STACK}\">${a.stop_price:,.2f}</span>",
                f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_SECONDARY}\">{distance_pct:.2f}%</span>",
            ])
        body_parts.append(section(
            title="Protected",
            glyph="●",
            body=data_table(
                headers=["Symbol", "Qty", "Side", "Last", "Stop", "Distance"],
                rows=rows,
            ),
        ))

    if closed:
        rows = [[
            f"<strong style=\"color:{_BAD};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
            f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
            severity_pill(a.position_side.value, "good" if a.position_side.value == "buy" else "bad"),
            f"<span style=\"font-family:{_MONO_STACK}\">${a.fill_estimate:,.2f}</span>",
        ] for a in closed]
        body_parts.append(section(
            title="Closed",
            glyph="◆",
            body=data_table(
                headers=["Symbol", "Qty", "Side", "Last"],
                rows=rows,
            ),
            severity="bad",
        ))

    if failed:
        rows = [[
            f"<strong style=\"color:{_BAD};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
            f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
            f"<span style=\"font-family:{_FONT_STACK};color:{_TEXT_PRIMARY}\">{a.error or ''}</span>",
        ] for a in failed]
        body_parts.append(section(
            title="Failed — needs manual review",
            glyph="⚠",
            body=data_table(headers=["Symbol", "Qty", "Error"], rows=rows),
            severity="bad",
        ))

    if deferred:
        rows = [[
            f"<strong style=\"color:{_WARN};font-family:{_FONT_STACK}\">{a.symbol}</strong>",
            f"<span style=\"font-family:{_MONO_STACK};color:{_TEXT_PRIMARY}\">{a.qty}</span>",
            severity_pill(a.position_side.value, "good" if a.position_side.value == "buy" else "bad"),
        ] for a in deferred]
        body_parts.append(section(
            title="Deferred to next session",
            glyph="◆",
            body=data_table(headers=["Symbol", "Qty", "Side"], rows=rows),
            severity="warn",
        ))

    subtitle = (
        f"{severity_pill('open positions', 'info')} "
        f"<span style=\"color:{_TEXT_SECONDARY};margin-left:8px\">"
        f"{len(protected)} protected · {len(closed)} closed · "
        f"{len(failed) + len(deferred)} need attention</span>"
    )

    return _shell(
        title="Open Positions — Auto-Protect Summary",
        subtitle_html=subtitle,
        body_html="".join(body_parts),
        accent=_ACCENT,
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
            f"<div style=\"margin-bottom:6px\">{severity_pill(p.severity, 'bad')} "
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
        f"{severity_pill('VIP tweet alert', 'bad')} "
        f"<span style=\"color:{_TEXT_SECONDARY};margin-left:8px\">"
        f"{len(posts)} high-severity post{'s' if len(posts) != 1 else ''}</span>"
    )
    return _shell(
        title="VIP Tweet Alert",
        subtitle_html=subtitle,
        body_html=body,
        accent=_BAD,
    )
