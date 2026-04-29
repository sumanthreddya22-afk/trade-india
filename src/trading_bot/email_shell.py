"""Shared visual helpers for all email types — Daily Digest, Midday
Snapshot, Action Alert, Strategy Promotion, Critical Alert.

Design strategy: **light-first with dark-mode overrides** (the
Apple/GitHub/Stripe pattern). Gmail iOS aggressively auto-inverts
"intentionally dark" emails into unreadable washed-out renders, so we:

  1. Ship a light-mode-correct email by default. Inline `bgcolor` HTML
     attributes + inline `style="background-color:..."` set the LIGHT
     palette explicitly. Gmail's invert algorithm doesn't fire.
  2. When the user's UI is in dark mode, our `<style>` block triggers
     dark-mode overrides via:
       * `@media (prefers-color-scheme: dark)`  → Apple Mail iOS/macOS,
         Outlook web (sometimes)
       * `[data-ogsb] / [data-ogsc]` selectors  → Gmail mobile dark UI.
         Gmail injects these data-attributes when in dark mode.
  3. The override CSS uses class-based selectors PLUS a `body *`
     wildcard for text, so any inline-styled element gets flipped.

Verified: Test C in the wireframe round confirmed Gmail mobile renders
this pattern correctly in both light and dark UI.

All inline CSS, table-based layout, 640-px max width, no external assets.
"""
from __future__ import annotations

from typing import Iterable, Literal


# ── LIGHT MODE PALETTE (defaults / inline values) ────────────────────
# All inline colors in helper output use these. Email is intentionally
# light by default — that's what defeats Gmail's auto-invert.
_BG_OUTER = "#f8fafc"        # slate-50 — outer canvas
_BG_CARD = "#ffffff"         # white   — card surface
_BG_TABLE_ROW = "#f1f5f9"    # slate-100 — zebra stripe
_BORDER = "#e2e8f0"          # slate-200

_TEXT_PRIMARY = "#0f172a"    # slate-900 (WCAG-AAA on white)
_TEXT_SECONDARY = "#334155"  # slate-700
_TEXT_MUTED = "#64748b"      # slate-500

_ACCENT = "#0891b2"          # cyan-600 — section labels
_ACCENT_BRIGHT = "#0e7490"   # cyan-700 — gradient stop
_GRADIENT_END = "#7c3aed"    # violet-600 — gradient end

_GOOD = "#16a34a"            # green-600
_GOOD_LIGHT = "#15803d"      # green-700 (darker on light bg for contrast)
_WARN = "#d97706"            # amber-600
_BAD = "#dc2626"             # red-600
_INFO = "#0284c7"            # sky-600

# ── DARK MODE PALETTE (used only inside the <style> block) ──────────
_DARK_BG_OUTER = "#0b1220"
_DARK_BG_CARD = "#111c2e"
_DARK_BG_TABLE_ROW = "#0f1828"
_DARK_BORDER = "#26334a"
_DARK_TEXT_PRIMARY = "#f1f5f9"
_DARK_TEXT_SECONDARY = "#cbd5e1"
_DARK_TEXT_MUTED = "#94a3b8"
_DARK_ACCENT = "#22d3ee"
_DARK_ACCENT_BRIGHT = "#67e8f9"
_DARK_GRADIENT_END = "#c4b5fd"
_DARK_GOOD = "#34d399"
_DARK_GOOD_LIGHT = "#6ee7b7"
_DARK_WARN = "#fbbf24"
_DARK_BAD = "#fb7185"
_DARK_INFO = "#7dd3fc"

_FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "'Inter','SF Pro Display',sans-serif"
)
_MONO_STACK = "'SF Mono','JetBrains Mono',Menlo,Consolas,monospace"

_STATUS_COLORS: dict[str, str] = {"ok": _GOOD, "warn": _WARN, "bad": _BAD}


# ── Atomic helpers ───────────────────────────────────────────────────

def pulse_dot(status: Literal["ok", "warn", "bad"]) -> str:
    color = _STATUS_COLORS.get(status, _INFO)
    return (
        f'<span style="display:inline-block;width:10px;height:10px;'
        f'border-radius:50%;background:{color};vertical-align:middle"></span>'
    )


def severity_pill(text: str, kind: Literal["good", "warn", "bad", "info", "neutral"] = "neutral") -> str:
    palette = {
        "good":    (_GOOD, "#dcfce7"),     # green-100
        "bad":     (_BAD,  "#fee2e2"),     # red-100
        "warn":    (_WARN, "#fef3c7"),     # amber-100
        "info":    (_INFO, "#e0f2fe"),     # sky-100
        "neutral": (_TEXT_SECONDARY, "#f1f5f9"),
    }
    fg, bg = palette.get(kind, palette["neutral"])
    return (
        f'<span class="pill pill-{kind}" '
        f'style="display:inline-block;padding:3px 9px;border-radius:999px;'
        f'background-color:{bg};color:{fg};font-size:10px;font-weight:600;'
        f'letter-spacing:1.4px;text-transform:uppercase;'
        f'font-family:{_FONT_STACK}">{text}</span>'
    )


def gradient_header(title: str, status: Literal["ok", "warn", "bad"],
                    timestamp_et: str) -> str:
    """Top of every email: thin gradient bar + title row + timestamp."""
    bar = (
        f'<div class="gradient-bar" style="height:4px;line-height:4px;font-size:0;'
        f'background-color:{_ACCENT};'
        f'background:linear-gradient(90deg,{_ACCENT_BRIGHT} 0%,{_GRADIENT_END} 100%)">'
        f'&nbsp;</div>'
    )
    title_row = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};margin-top:18px"><tr>'
        f'<td class="bg-outer text-primary" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};padding:0 24px;'
        f'color:{_TEXT_PRIMARY};font-family:{_FONT_STACK};font-size:22px;'
        f'font-weight:700;letter-spacing:-0.01em">{title} {pulse_dot(status)}</td>'
        f'<td class="bg-outer text-muted" align="right" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};padding:0 24px;color:{_TEXT_MUTED};'
        f'font-family:{_FONT_STACK};font-size:12px">{timestamp_et}</td>'
        f'</tr></table>'
    )
    return bar + title_row


def kpi_card(*, label: str, value: str, delta: str | None = None,
             delta_kind: Literal["good", "bad", "neutral"] = "neutral",
             value_color: str | None = None,
             sub: str | None = None,
             sparkline_html: str | None = None) -> str:
    """Single KPI tile."""
    _vcolor = value_color if value_color else _TEXT_PRIMARY
    delta_html = ""
    if delta:
        delta_color = {"good": _GOOD, "bad": _BAD,
                       "neutral": _TEXT_SECONDARY}.get(delta_kind, _TEXT_SECONDARY)
        delta_class = {"good": "text-good", "bad": "text-bad",
                       "neutral": "text-secondary"}.get(delta_kind, "text-secondary")
        delta_html = (
            f'<div class="{delta_class}" style="color:{delta_color};font-size:13px;'
            f'font-weight:600;margin-top:4px;font-family:{_MONO_STACK}">{delta}</div>'
        )
    sub_html = (
        f'<div class="text-muted" style="color:{_TEXT_MUTED};font-size:12px;'
        f'margin-top:4px;font-family:{_FONT_STACK}">{sub}</div>'
        if sub else ""
    )
    sparkline_block = (
        f'<div style="margin-top:8px">{sparkline_html}</div>'
        if sparkline_html else ""
    )
    return (
        f'<td valign="top" class="card border" bgcolor="{_BG_CARD}" '
        f'style="padding:16px 18px;background-color:{_BG_CARD};'
        f'border:1px solid {_BORDER};border-radius:12px;width:25%">'
        f'<div class="text-accent" style="color:{_ACCENT};font-size:10px;'
        f'letter-spacing:1.4px;text-transform:uppercase;font-weight:600;'
        f'font-family:{_FONT_STACK}">{label}</div>'
        f'<div class="text-primary" style="color:{_vcolor};font-size:28px;'
        f'font-weight:700;margin-top:8px;line-height:1.1;letter-spacing:-0.02em;'
        f'font-family:{_MONO_STACK}">{value}</div>'
        f'{delta_html}{sub_html}{sparkline_block}</td>'
    )


def kpi_grid(cards: list[str]) -> str:
    """Lay 4 cards in a row. Pad with blanks if fewer."""
    while len(cards) < 4:
        cards.append('<td style="width:25%"></td>')
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="8" border="0" '
        f'width="100%" class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};margin:18px 0">'
        f'<tr>{"".join(cards)}</tr></table>'
    )


def progress_bar(*, value_pct: float, color: str, label: str) -> str:
    """Horizontal progress bar. Clamped to [0, 100]."""
    pct = max(0.0, min(100.0, value_pct))
    return (
        f'<div style="margin:6px 0">'
        f'<div style="display:flex;justify-content:space-between;'
        f'color:{_TEXT_SECONDARY};font-size:11px;font-family:{_FONT_STACK};'
        f'margin-bottom:4px">'
        f'<span class="text-secondary">{label}</span>'
        f'<span class="text-secondary">{value_pct:.1f}%</span></div>'
        f'<div class="progress-track" style="background-color:{_BORDER};'
        f'border-radius:999px;height:6px;overflow:hidden">'
        f'<div style="width:{pct:g}%;height:100%;background-color:{color};'
        f'border-radius:999px"></div></div></div>'
    )


def sparkline_svg(values: Iterable[float], *, width: int = 120, height: int = 32,
                  color: str = _ACCENT) -> str:
    vs = list(values)
    if not vs:
        return f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"></svg>'
    lo, hi = min(vs), max(vs)
    rng = hi - lo if hi > lo else 1.0
    n = len(vs)
    if n == 1:
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
    """Section card with title + body, color-coded left rail."""
    color = {"good": _GOOD, "warn": _WARN, "bad": _BAD,
             "info": _INFO, "neutral": _ACCENT}.get(severity, _ACCENT)
    rail_class = f"rail-{severity}"
    body_bg = _BG_TABLE_ROW
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};margin:18px 16px 0;width:auto">'
        f'<tr><td class="card border {rail_class}" bgcolor="{_BG_CARD}" '
        f'style="background-color:{_BG_CARD};border:1px solid {_BORDER};'
        f'border-left:3px solid {color};border-radius:10px;padding:14px 18px">'
        f'<div style="margin-bottom:10px">'
        f'<span class="rail-glyph" style="color:{color};font-size:14px;margin-right:8px;'
        f'vertical-align:middle">{glyph}</span>'
        f'<span class="rail-label" style="color:{color};font-size:11px;font-weight:700;'
        f'letter-spacing:1.4px;text-transform:uppercase;'
        f'font-family:{_FONT_STACK};vertical-align:middle">{title}</span></div>'
        f'<div class="text-primary table-row-bg" style="color:{_TEXT_PRIMARY};'
        f'font-family:{_FONT_STACK};font-size:14px;line-height:1.55;'
        f'background-color:{body_bg};padding:10px 12px;border-radius:6px">{body}</div>'
        f'</td></tr></table>'
    )


def data_table(*, headers: list[str], rows: list[list[str]],
               right_align_cols: list[int] | None = None) -> str:
    right = set(right_align_cols or [])
    th = "".join(
        f'<th class="text-accent border" '
        f'style="text-align:{"right" if i in right else "left"};'
        f'padding:10px 12px;color:{_ACCENT};font-size:10px;letter-spacing:1.2px;'
        f'text-transform:uppercase;font-weight:600;border-bottom:1px solid {_BORDER};'
        f'font-family:{_FONT_STACK};background-color:{_BG_CARD}">{h}</th>'
        for i, h in enumerate(headers)
    )
    body_rows = []
    for ri, row in enumerate(rows):
        bg = _BG_TABLE_ROW if ri % 2 else _BG_CARD
        row_class = "table-row-alt" if ri % 2 else "table-row"
        cells = "".join(
            f'<td class="text-primary border {row_class}" bgcolor="{bg}" '
            f'style="text-align:{"right" if i in right else "left"};'
            f'padding:10px 12px;color:{_TEXT_PRIMARY};font-size:13px;'
            f'background-color:{bg};'
            f'border-bottom:1px solid {_BORDER};font-family:{_FONT_STACK}">{cell}</td>'
            for i, cell in enumerate(row)
        )
        body_rows.append(f'<tr>{cells}</tr>')
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" class="card border" bgcolor="{_BG_CARD}" '
        f'style="border-collapse:collapse;background-color:{_BG_CARD};'
        f'border:1px solid {_BORDER};border-radius:12px;overflow:hidden">'
        f'<thead><tr>{th}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>'
    )


def empty_state(text: str, *, card_radius: str = "12px") -> str:
    return (
        f'<div class="card border text-muted" '
        f'style="padding:18px;background-color:{_BG_CARD};border:1px dashed {_BORDER};'
        f'border-radius:{card_radius};color:{_TEXT_MUTED};font-size:13px;'
        f'text-align:center;font-family:{_FONT_STACK}">{text}</div>'
    )


def footer(*, version: str, git_sha: str, dashboard_url: str | None = None) -> str:
    link = ""
    if dashboard_url:
        link = (
            f' &middot; <a class="text-accent-bright" href="{dashboard_url}" '
            f'style="color:{_ACCENT_BRIGHT};text-decoration:none">view dashboard →</a>'
        )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" class="bg-outer border" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};margin:32px 0 0;border-top:1px solid {_BORDER}">'
        f'<tr><td class="text-muted" '
        f'style="padding:14px 24px;color:{_TEXT_MUTED};font-size:11px;'
        f'font-family:{_FONT_STACK}">'
        f'{version} &middot; {git_sha}{link}</td></tr></table>'
    )


# ── The shell ─────────────────────────────────────────────────────────

def _build_dark_overrides() -> str:
    """Generate the dark-mode CSS rules. Applied via:
       1. @media (prefers-color-scheme: dark)  → Apple Mail
       2. [data-ogsb] / [data-ogsc] selectors → Gmail mobile dark UI
    Both layers reference the same class names emitted by the helpers."""
    rules = (
        # Backgrounds
        f"body, .bg-outer {{ background-color: {_DARK_BG_OUTER} !important; }} "
        f".card {{ background-color: {_DARK_BG_CARD} !important; }} "
        f".table-row, thead th {{ background-color: {_DARK_BG_CARD} !important; }} "
        f".table-row-alt, .table-row-bg {{ background-color: {_DARK_BG_TABLE_ROW} !important; }} "
        f".border {{ border-color: {_DARK_BORDER} !important; }} "
        f".progress-track {{ background-color: {_DARK_BORDER} !important; }} "
        # Text — use class targeting (specificity beats wildcard)
        f".text-primary, .text-primary * {{ color: {_DARK_TEXT_PRIMARY} !important; }} "
        f".text-secondary, .text-secondary * {{ color: {_DARK_TEXT_SECONDARY} !important; }} "
        f".text-muted, .text-muted * {{ color: {_DARK_TEXT_MUTED} !important; }} "
        f".text-accent {{ color: {_DARK_ACCENT} !important; }} "
        f".text-accent-bright {{ color: {_DARK_ACCENT_BRIGHT} !important; }} "
        f".text-good {{ color: {_DARK_GOOD} !important; }} "
        f".text-bad {{ color: {_DARK_BAD} !important; }} "
        f".text-warn {{ color: {_DARK_WARN} !important; }} "
        # Severity rails / pills
        f".rail-good, .rail-good .rail-glyph, .rail-good .rail-label "
        f"{{ color: {_DARK_GOOD} !important; }} "
        f".rail-bad, .rail-bad .rail-glyph, .rail-bad .rail-label "
        f"{{ color: {_DARK_BAD} !important; }} "
        f".rail-warn, .rail-warn .rail-glyph, .rail-warn .rail-label "
        f"{{ color: {_DARK_WARN} !important; }} "
        f".rail-info, .rail-info .rail-glyph, .rail-info .rail-label "
        f"{{ color: {_DARK_INFO} !important; }} "
        f".rail-neutral, .rail-neutral .rail-glyph, .rail-neutral .rail-label "
        f"{{ color: {_DARK_ACCENT} !important; }} "
        # Pills get their own bg/text overrides for dark mode
        f".pill-good {{ background-color: #064e3b !important; color: {_DARK_GOOD_LIGHT} !important; }} "
        f".pill-bad {{ background-color: #7f1d1d !important; color: {_DARK_BAD} !important; }} "
        f".pill-warn {{ background-color: #78350f !important; color: {_DARK_WARN} !important; }} "
        f".pill-info {{ background-color: #0c4a6e !important; color: {_DARK_INFO} !important; }} "
        f".pill-neutral {{ background-color: #1e293b !important; color: {_DARK_TEXT_SECONDARY} !important; }} "
        # Anchors
        f"a {{ color: {_DARK_ACCENT_BRIGHT} !important; }} "
    )
    return rules


def render_shell(*, title: str, status: Literal["ok", "warn", "bad"],
                 timestamp_et: str, body_sections: list[str]) -> str:
    """Top-level email envelope. Light-mode-first; dark via media query
    + Gmail mobile data-attribute selectors."""
    body_html = "".join(body_sections)
    dark_css = _build_dark_overrides()

    head = (
        '<!DOCTYPE html><html lang="en">'
        '<head>'
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width,initial-scale=1" />'
        # Tell every client we support BOTH schemes — required for media
        # query to fire and for Gmail mobile to inject [data-ogsb]/[data-ogsc].
        '<meta name="color-scheme" content="light dark" />'
        '<meta name="supported-color-schemes" content="light dark" />'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge" />'
        f'<title>{title}</title>'
        '<style type="text/css">'
        ':root { color-scheme: light dark; supported-color-schemes: light dark; } '
        # Light-mode default
        f'body, table, td {{ background-color: {_BG_OUTER}; color: {_TEXT_PRIMARY}; }} '
        f'a {{ color: {_ACCENT_BRIGHT}; text-decoration: none; }} '
        # Apple Mail dark mode
        f'@media (prefers-color-scheme: dark) {{ {dark_css} }} '
        # Gmail mobile dark UI (iOS + Android Gmail app)
        f'[data-ogsb] body, [data-ogsb] .bg-outer {{ background-color: {_DARK_BG_OUTER} !important; }} '
        f'[data-ogsb] .card {{ background-color: {_DARK_BG_CARD} !important; }} '
        f'[data-ogsb] .table-row, [data-ogsb] thead th {{ background-color: {_DARK_BG_CARD} !important; }} '
        f'[data-ogsb] .table-row-alt, [data-ogsb] .table-row-bg {{ background-color: {_DARK_BG_TABLE_ROW} !important; }} '
        f'[data-ogsb] .border {{ border-color: {_DARK_BORDER} !important; }} '
        f'[data-ogsb] .progress-track {{ background-color: {_DARK_BORDER} !important; }} '
        # Gmail text overrides
        f'[data-ogsc] .text-primary, [data-ogsc] .text-primary * {{ color: {_DARK_TEXT_PRIMARY} !important; }} '
        f'[data-ogsc] .text-secondary, [data-ogsc] .text-secondary * {{ color: {_DARK_TEXT_SECONDARY} !important; }} '
        f'[data-ogsc] .text-muted, [data-ogsc] .text-muted * {{ color: {_DARK_TEXT_MUTED} !important; }} '
        f'[data-ogsc] .text-accent {{ color: {_DARK_ACCENT} !important; }} '
        f'[data-ogsc] .text-accent-bright {{ color: {_DARK_ACCENT_BRIGHT} !important; }} '
        f'[data-ogsc] .text-good {{ color: {_DARK_GOOD} !important; }} '
        f'[data-ogsc] .text-bad {{ color: {_DARK_BAD} !important; }} '
        f'[data-ogsc] .text-warn {{ color: {_DARK_WARN} !important; }} '
        f'[data-ogsc] .rail-good, [data-ogsc] .rail-good .rail-glyph, [data-ogsc] .rail-good .rail-label {{ color: {_DARK_GOOD} !important; }} '
        f'[data-ogsc] .rail-bad, [data-ogsc] .rail-bad .rail-glyph, [data-ogsc] .rail-bad .rail-label {{ color: {_DARK_BAD} !important; }} '
        f'[data-ogsc] .rail-warn, [data-ogsc] .rail-warn .rail-glyph, [data-ogsc] .rail-warn .rail-label {{ color: {_DARK_WARN} !important; }} '
        f'[data-ogsc] .rail-info, [data-ogsc] .rail-info .rail-glyph, [data-ogsc] .rail-info .rail-label {{ color: {_DARK_INFO} !important; }} '
        f'[data-ogsc] .rail-neutral, [data-ogsc] .rail-neutral .rail-glyph, [data-ogsc] .rail-neutral .rail-label {{ color: {_DARK_ACCENT} !important; }} '
        f'[data-ogsc] .pill-good {{ background-color: #064e3b !important; color: {_DARK_GOOD_LIGHT} !important; }} '
        f'[data-ogsc] .pill-bad {{ background-color: #7f1d1d !important; color: {_DARK_BAD} !important; }} '
        f'[data-ogsc] .pill-warn {{ background-color: #78350f !important; color: {_DARK_WARN} !important; }} '
        f'[data-ogsc] .pill-info {{ background-color: #0c4a6e !important; color: {_DARK_INFO} !important; }} '
        f'[data-ogsc] .pill-neutral {{ background-color: #1e293b !important; color: {_DARK_TEXT_SECONDARY} !important; }} '
        f'[data-ogsc] a {{ color: {_DARK_ACCENT_BRIGHT} !important; }} '
        '</style>'
        '</head>'
    )

    body_open = (
        f'<body class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="margin:0;padding:0;background-color:{_BG_OUTER};'
        f'color:{_TEXT_PRIMARY};font-family:{_FONT_STACK};'
        '-webkit-text-size-adjust:100%;'
        'color-scheme:light dark;supported-color-schemes:light dark">'
    )
    return (
        head + body_open
        + '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};margin:0;padding:0">'
        f'<tr><td class="bg-outer" align="center" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER};padding:16px 8px">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="640" class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="max-width:640px;width:100%;background-color:{_BG_OUTER};'
        f'border-radius:12px;overflow:hidden">'
        f'<tr><td class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER}">{gradient_header(title, status, timestamp_et)}</td></tr>'
        f'<tr><td class="bg-outer" bgcolor="{_BG_OUTER}" '
        f'style="background-color:{_BG_OUTER}">{body_html}</td></tr>'
        '</table></td></tr></table>'
        '</body></html>'
    )
