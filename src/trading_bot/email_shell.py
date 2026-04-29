"""Shared visual helpers for all email types — Daily Digest, Midday
Snapshot, Action Alert, Strategy Promotion. Mirrors dashboard color
tokens (#0f172a card, #06b6d4 cyan, #e2e8f0 text) and adds shell
elements (gradient brand bar, pulse-dot status, sparklines via inline
SVG, progress bars). All inline CSS, table-based layout, 640px max.
"""
from __future__ import annotations

from typing import Iterable, Literal


# ── Color tokens ─────────────────────────────────────────────────────
_BG_OUTER = "#0a0f1c"
_BG_CARD = "#0f172a"
_BORDER = "#1e293b"
_TEXT_PRIMARY = "#e2e8f0"
_TEXT_SECONDARY = "#94a3b8"
_TEXT_MUTED = "#64748b"

_ACCENT = "#06b6d4"          # cyan-500 — section labels
_ACCENT_BRIGHT = "#22d3ee"   # cyan-400 — gradient stop
_GRADIENT_END = "#a78bfa"    # purple-400 — gradient end (matches dashboard)

_GOOD = "#10b981"
_GOOD_LIGHT = "#34d399"
_WARN = "#fbbf24"
_BAD = "#fb7185"
_INFO = "#60a5fa"

_FONT_STACK = (
    "'Inter','SF Pro Display','-apple-system',BlinkMacSystemFont,"
    "Segoe UI,Roboto,sans-serif"
)
_MONO_STACK = "'SF Mono','JetBrains Mono',Menlo,Consolas,monospace"

_STATUS_COLORS: dict[str, str] = {"ok": _GOOD, "warn": _WARN, "bad": _BAD}


# ── Atomic helpers ───────────────────────────────────────────────────

def pulse_dot(status: Literal["ok", "warn", "bad"]) -> str:
    color = _STATUS_COLORS.get(status, _INFO)
    return (
        f'<span style="display:inline-block;width:10px;height:10px;'
        f'border-radius:50%;background:{color};box-shadow:0 0 10px {color};'
        f'vertical-align:middle"></span>'
    )


def severity_pill(text: str, kind: Literal["good", "warn", "bad", "info", "neutral"] = "neutral") -> str:
    palette = {
        "good":    (_GOOD_LIGHT, "rgba(16,185,129,0.18)"),
        "bad":     (_BAD,        "rgba(251,113,133,0.18)"),
        "warn":    (_WARN,       "rgba(251,191,36,0.18)"),
        "info":    (_INFO,       "rgba(96,165,250,0.18)"),
        "neutral": (_TEXT_SECONDARY, "rgba(148,163,184,0.12)"),
    }
    fg, bg = palette.get(kind, palette["neutral"])
    return (
        f'<span style="display:inline-block;padding:3px 9px;border-radius:999px;'
        f'background:{bg};color:{fg};font-size:10px;font-weight:600;'
        f'letter-spacing:1.4px;text-transform:uppercase;'
        f'font-family:{_FONT_STACK}">{text}</span>'
    )


def gradient_header(title: str, status: Literal["ok", "warn", "bad"],
                    timestamp_et: str) -> str:
    """Brand bar + title + pulse-dot + timestamp. Renders as the top of
    every email. Uses a 6px gradient bar above the title row."""
    bar = (
        f'<div style="height:6px;background:linear-gradient(90deg,'
        f'{_ACCENT_BRIGHT} 0%,{_GRADIENT_END} 100%)"></div>'
    )
    title_row = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="margin-top:18px"><tr>'
        f'<td style="padding:0 24px"><span style="color:{_TEXT_PRIMARY};'
        f'font-family:{_FONT_STACK};font-size:22px;font-weight:700;'
        f'letter-spacing:-0.01em">{title}</span> {pulse_dot(status)}</td>'
        f'<td align="right" style="padding:0 24px;color:{_TEXT_MUTED};'
        f'font-family:{_FONT_STACK};font-size:12px">{timestamp_et}</td>'
        f'</tr></table>'
    )
    return bar + title_row


def kpi_card(*, label: str, value: str, delta: str | None = None,
             delta_kind: Literal["good", "bad", "neutral"] = "neutral",
             sparkline_html: str | None = None) -> str:
    delta_html = ""
    if delta:
        delta_color = {"good": _GOOD_LIGHT, "bad": _BAD,
                       "neutral": _TEXT_SECONDARY}.get(delta_kind, _TEXT_SECONDARY)
        delta_html = (
            f'<div style="color:{delta_color};font-size:13px;font-weight:600;'
            f'margin-top:4px;font-family:{_MONO_STACK}">{delta}</div>'
        )
    sparkline_block = (
        f'<div style="margin-top:8px">{sparkline_html}</div>'
        if sparkline_html else ""
    )
    return (
        f'<td valign="top" style="padding:16px 18px;background:{_BG_CARD};'
        f'border:1px solid {_BORDER};border-radius:12px;width:25%">'
        f'<div style="color:{_ACCENT};font-size:10px;letter-spacing:1.4px;'
        f'text-transform:uppercase;font-weight:600;'
        f'font-family:{_FONT_STACK}">{label}</div>'
        f'<div style="color:{_TEXT_PRIMARY};font-size:28px;font-weight:700;'
        f'margin-top:8px;line-height:1.1;letter-spacing:-0.02em;'
        f'font-family:{_MONO_STACK}">{value}</div>'
        f'{delta_html}{sparkline_block}</td>'
    )


def kpi_grid(cards: list[str]) -> str:
    """Lay 4 cards in a row. Pad with blanks if fewer."""
    while len(cards) < 4:
        cards.append('<td style="width:25%"></td>')
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="8" border="0" '
        f'width="100%" style="margin:18px 0"><tr>{"".join(cards)}</tr></table>'
    )


def progress_bar(*, value_pct: float, color: str, label: str) -> str:
    """Horizontal progress bar. Clamped to [0, 100]."""
    pct = max(0.0, min(100.0, value_pct))
    return (
        f'<div style="margin:6px 0">'
        f'<div style="display:flex;justify-content:space-between;'
        f'color:{_TEXT_SECONDARY};font-size:11px;font-family:{_FONT_STACK};'
        f'margin-bottom:4px">'
        f'<span>{label}</span><span>{value_pct:.1f}%</span></div>'
        f'<div style="background:{_BORDER};border-radius:999px;height:6px;'
        f'overflow:hidden">'
        f'<div style="width:{pct:g}%;height:100%;background:{color};'
        f'border-radius:999px"></div></div></div>'
    )


def sparkline_svg(values: Iterable[float], *, width: int = 120, height: int = 32,
                  color: str = _ACCENT_BRIGHT) -> str:
    vs = list(values)
    if not vs:
        return f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"></svg>'
    lo, hi = min(vs), max(vs)
    rng = hi - lo if hi > lo else 1.0
    n = len(vs)
    if n == 1:
        # Single point — draw a flat line
        y = height / 2
        points = f'0,{y:.2f} {width},{y:.2f}'
    else:
        step = width / (n - 1)
        points = " ".join(
            f"{i * step:.2f},{(height - 4) - ((v - lo) / rng) * (height - 8):.2f}"
            for i, v in enumerate(vs)
        )
    return (
        f'<svg width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:block">'
        f'<polyline points="{points}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />'
        f'</svg>'
    )


def section(*, title: str, glyph: str, body: str,
            severity: Literal["good", "warn", "bad", "info", "neutral"] = "neutral") -> str:
    color = {"good": _GOOD_LIGHT, "warn": _WARN, "bad": _BAD,
             "info": _INFO, "neutral": _ACCENT}.get(severity, _ACCENT)
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="margin:24px 0 0">'
        f'<tr><td style="padding:0 24px 10px">'
        f'<span style="color:{color};font-size:14px;margin-right:8px">{glyph}</span>'
        f'<span style="color:{color};font-size:11px;font-weight:600;'
        f'letter-spacing:1.4px;text-transform:uppercase;'
        f'font-family:{_FONT_STACK}">{title}</span></td></tr>'
        f'<tr><td style="padding:0 24px">{body}</td></tr></table>'
    )


def data_table(*, headers: list[str], rows: list[list[str]],
               right_align_cols: list[int] | None = None) -> str:
    right = set(right_align_cols or [])
    th = "".join(
        f'<th style="text-align:{"right" if i in right else "left"};'
        f'padding:10px 12px;color:{_ACCENT};font-size:10px;letter-spacing:1.2px;'
        f'text-transform:uppercase;font-weight:600;border-bottom:1px solid {_BORDER};'
        f'font-family:{_FONT_STACK}">{h}</th>'
        for i, h in enumerate(headers)
    )
    body_rows = []
    for ri, row in enumerate(rows):
        bg = "rgba(15,23,42,0.4)" if ri % 2 else "transparent"
        cells = "".join(
            f'<td style="text-align:{"right" if i in right else "left"};'
            f'padding:10px 12px;color:{_TEXT_PRIMARY};font-size:13px;'
            f'border-bottom:1px solid {_BORDER};font-family:{_FONT_STACK}">{cell}</td>'
            for i, cell in enumerate(row)
        )
        body_rows.append(f'<tr style="background:{bg}">{cells}</tr>')
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;background:{_BG_CARD};'
        f'border:1px solid {_BORDER};border-radius:12px;overflow:hidden">'
        f'<thead><tr>{th}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>'
    )


def footer(*, version: str, git_sha: str, dashboard_url: str | None = None) -> str:
    link = ""
    if dashboard_url:
        link = (
            f' &middot; <a href="{dashboard_url}" '
            f'style="color:{_ACCENT_BRIGHT};text-decoration:none">view dashboard →</a>'
        )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="margin:32px 0 0;border-top:1px solid {_BORDER}">'
        f'<tr><td style="padding:14px 24px;color:{_TEXT_MUTED};font-size:11px;'
        f'font-family:{_FONT_STACK}">'
        f'{version} &middot; {git_sha}{link}</td></tr></table>'
    )


def render_shell(*, title: str, status: Literal["ok", "warn", "bad"],
                 timestamp_et: str, body_sections: list[str]) -> str:
    """Top-level email envelope. Wraps everything in a 640-px max-width
    table with the dashboard's outer background color."""
    body_html = "".join(body_sections)
    return (
        f'<!DOCTYPE html><html><head>'
        f'<meta charset="utf-8" />'
        f'<title>{title}</title>'
        f'</head><body style="margin:0;padding:0;background:{_BG_OUTER};'
        f'font-family:{_FONT_STACK}">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{_BG_OUTER}"><tr><td align="center">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="640" style="max-width:640px;width:100%;background:{_BG_OUTER}">'
        f'<tr><td>{gradient_header(title, status, timestamp_et)}</td></tr>'
        f'<tr><td>{body_html}</td></tr>'
        f'</table></td></tr></table></body></html>'
    )
