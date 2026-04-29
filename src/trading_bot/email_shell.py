"""Shared visual helpers for all email types — Daily Digest, Midday
Snapshot, Action Alert, Strategy Promotion.

Dark-mode-native design (Gmail/Apple Mail/Outlook all show this email
as dark even in their light-mode UI). High-contrast text on near-black
backgrounds; we explicitly tell the client to NOT invert the email
via the `color-scheme: dark` meta. Tokens deliberately use higher
luminance than CSS-typical (#f1f5f9 vs #e2e8f0) so Gmail's mobile
inversion heuristic (which kicks in when contrast looks "low") leaves
us alone.

All inline CSS, table-based layout, 640px max.
"""
from __future__ import annotations

from typing import Iterable, Literal


# ── Color tokens ─────────────────────────────────────────────────────
# Backgrounds — dark navy spectrum, not pure black (Gmail's inversion
# heuristic occasionally flips pure black/white pairs). Card slightly
# lighter than outer for visual depth.
_BG_OUTER = "#0b1220"
_BG_CARD = "#111c2e"
_BG_TABLE_ROW = "#0f1828"  # subtle stripe in data tables
_BORDER = "#26334a"        # high-enough contrast border that's visible

# Text — bright enough for readability, prevents Gmail "low-contrast"
# auto-invert. Primary is near-white, secondary is light-slate, muted
# is mid-slate (still legible — never goes below WCAG-AA 4.5:1).
_TEXT_PRIMARY = "#f1f5f9"      # WCAG-AAA on _BG_CARD
_TEXT_SECONDARY = "#cbd5e1"    # WCAG-AA on _BG_CARD
_TEXT_MUTED = "#94a3b8"        # for footer/timestamps only

_ACCENT = "#22d3ee"          # cyan-400 — section labels (brighter)
_ACCENT_BRIGHT = "#67e8f9"   # cyan-300 — gradient stop
_GRADIENT_END = "#c4b5fd"    # violet-300 — gradient end

_GOOD = "#34d399"
_GOOD_LIGHT = "#6ee7b7"
_WARN = "#fbbf24"
_BAD = "#fb7185"
_INFO = "#7dd3fc"

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
             value_color: str | None = None,
             sub: str | None = None,
             sparkline_html: str | None = None) -> str:
    """Single KPI tile.

    ``value_color`` overrides the default text color on the value.
    ``sub``         renders a small muted line below the value (replaces delta
                    in legacy callers).
    ``delta`` / ``delta_kind`` render a colored delta row (newer callers).
    """
    _vcolor = value_color if value_color else _TEXT_PRIMARY
    delta_html = ""
    if delta:
        delta_color = {"good": _GOOD_LIGHT, "bad": _BAD,
                       "neutral": _TEXT_SECONDARY}.get(delta_kind, _TEXT_SECONDARY)
        delta_html = (
            f'<div style="color:{delta_color};font-size:13px;font-weight:600;'
            f'margin-top:4px;font-family:{_MONO_STACK}">{delta}</div>'
        )
    sub_html = (
        f'<div style="color:{_TEXT_MUTED};font-size:12px;margin-top:4px;'
        f'font-family:{_FONT_STACK}">{sub}</div>'
        if sub else ""
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
        f'<div style="color:{_vcolor};font-size:28px;font-weight:700;'
        f'margin-top:8px;line-height:1.1;letter-spacing:-0.02em;'
        f'font-family:{_MONO_STACK}">{value}</div>'
        f'{delta_html}{sub_html}{sparkline_block}</td>'
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
    """Section with title + body, wrapped in a visible card so the
    content is clearly delineated even in clients that strip our styles."""
    color = {"good": _GOOD_LIGHT, "warn": _WARN, "bad": _BAD,
             "info": _INFO, "neutral": _ACCENT}.get(severity, _ACCENT)
    bg_tint = {
        "good":    "rgba(52,211,153,0.06)",
        "warn":    "rgba(251,191,36,0.06)",
        "bad":     "rgba(251,113,133,0.06)",
        "info":    "rgba(125,211,252,0.06)",
        "neutral": "rgba(34,211,238,0.04)",
    }.get(severity, "rgba(34,211,238,0.04)")
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="margin:18px 16px 0;width:auto"><tr><td '
        f'style="background:{_BG_CARD};border:1px solid {_BORDER};'
        f'border-left:3px solid {color};border-radius:10px;padding:14px 18px">'
        f'<div style="margin-bottom:10px">'
        f'<span style="color:{color};font-size:14px;margin-right:8px;'
        f'vertical-align:middle">{glyph}</span>'
        f'<span style="color:{color};font-size:11px;font-weight:700;'
        f'letter-spacing:1.4px;text-transform:uppercase;'
        f'font-family:{_FONT_STACK};vertical-align:middle">{title}</span></div>'
        f'<div style="color:{_TEXT_PRIMARY};font-family:{_FONT_STACK};'
        f'font-size:14px;line-height:1.55;background:{bg_tint};'
        f'padding:10px 12px;border-radius:6px">{body}</div>'
        f'</td></tr></table>'
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


def empty_state(text: str, *, card_radius: str = "12px") -> str:
    """Placeholder panel for sections with no data."""
    return (
        f'<div style="padding:18px;background:{_BG_CARD};border:1px dashed {_BORDER};'
        f'border-radius:{card_radius};color:{_TEXT_MUTED};font-size:13px;text-align:center;'
        f'font-family:{_FONT_STACK}">{text}</div>'
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
    """Top-level email envelope. Dark-mode-native: declares color-scheme
    so Gmail/Apple Mail don't auto-invert into a low-contrast mess.
    640-px max-width table layout. Inline CSS only (most clients strip
    <style>)."""
    body_html = "".join(body_sections)
    # Explicit color-scheme + supported-color-schemes makes mobile clients
    # leave the dark theme alone instead of running their inversion heuristic
    # (the original cause of the unreadable washed-out look in Gmail mobile).
    head = (
        '<!DOCTYPE html><html lang="en">'
        '<head>'
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width,initial-scale=1" />'
        '<meta name="color-scheme" content="dark only" />'
        '<meta name="supported-color-schemes" content="dark" />'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge" />'
        f'<title>{title}</title>'
        # Apple Mail + iOS Mail honor this for true dark rendering.
        '<style>'
        ':root { color-scheme: dark; supported-color-schemes: dark; } '
        f'body, table, td {{ background: {_BG_OUTER} !important; }} '
        f'a {{ color: {_ACCENT_BRIGHT} !important; }} '
        # Force common Gmail/Outlook overrides to respect our palette.
        '@media (prefers-color-scheme: light) { '
        f'  body, table, td {{ background: {_BG_OUTER} !important; }} '
        f'  .text-primary {{ color: {_TEXT_PRIMARY} !important; }} '
        '}'
        '</style>'
        '</head>'
    )
    body_open = (
        f'<body style="margin:0;padding:0;background:{_BG_OUTER};'
        f'color:{_TEXT_PRIMARY};font-family:{_FONT_STACK};'
        # tells Gmail iOS/Android: this email is intentionally dark, do not invert
        '-webkit-text-size-adjust:100%;'
        'color-scheme:dark;supported-color-schemes:dark">'
    )
    return (
        head + body_open
        + '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background:{_BG_OUTER};margin:0;padding:0">'
        '<tr><td align="center" style="padding:16px 8px">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="640" style="max-width:640px;width:100%;background:{_BG_OUTER};'
        f'border-radius:12px;overflow:hidden">'
        f'<tr><td>{gradient_header(title, status, timestamp_et)}</td></tr>'
        f'<tr><td>{body_html}</td></tr>'
        '</table></td></tr></table>'
        '</body></html>'
    )
