"""Style 2 'Dashboard Card' rebuilt with fortress-dark — bulletproof
against Gmail mobile's auto-invert. Pattern used by Stripe, Linear,
Vercel for their transactional emails:

  * `bgcolor` HTML attribute on every table/td (Gmail respects this
    MORE than CSS for inversion decisions)
  * inline style `background-color:#X !important` redundantly
  * NO rgba() / transparent backgrounds (Gmail flags those for invert)
  * `[data-ogsb]` and `[data-ogsc]` Gmail-mobile-specific selectors
  * Outlook MSO conditional CSS for desktop Outlook
  * Every text color set explicitly via `<font color>` AND inline style

The design itself is unchanged from the wireframe — only the
plumbing under the colors is hardened.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ============================================================================
# Sample data (same as the original wireframe)
# ============================================================================

DATA = {
    "date_long": "Wednesday, April 29 2026",
    "date_short": "Apr 29",
    "timestamp": "16:30 ET",
    "equity_open": 14_926.18,
    "equity_close": 14_988.51,
    "equity_pnl": 62.33,
    "equity_pnl_pct": 0.42,
    "realized_pnl": 18.50,
    "unrealized_pnl": 43.83,
    "regime": "trending_up",
    "vix": 18.02,
    "trades_count": 3,
    "trades_buys": 2,
    "win_rate_7d": 0.71,
    "trades": [
        {"time": "10:32", "side": "BUY", "symbol": "DELL",
         "qty": "2", "price": "215.97", "outcome": "open"},
        {"time": "11:14", "side": "BUY", "symbol": "MRVL",
         "qty": "8", "price": "62.84", "outcome": "open"},
        {"time": "13:48", "side": "SELL", "symbol": "GOOGL",
         "qty": "3", "price": "172.45", "outcome": "exit +1.8%"},
    ],
    "equity_30d": [
        14400, 14380, 14420, 14470, 14510, 14550, 14530, 14510, 14490,
        14530, 14580, 14620, 14600, 14650, 14700, 14730, 14710, 14760,
        14800, 14850, 14820, 14860, 14900, 14920, 14910, 14930, 14950,
        14920, 14926, 14988,
    ],
    "session_review": {
        "well": [
            "Day P&L positive: +0.42% (+$62.33)",
            "3 trade decisions executed (2 buys, 1 exit)",
            "7d win rate 71% (5/7 closed)",
            "Zero runtime errors logged",
        ],
        "wrong": [],
        "improve": [
            "VIX at 18.0 — moderate; no action needed",
            "AAPL + MSFT blocked by earnings gate (both Q3 within 5d)",
            "Wheel cycles 0 — IV history < 5 days, signals firing as expected",
        ],
    },
}


# ============================================================================
# Color palette — solid hex only, no rgba (Gmail flags transparent for invert)
# ============================================================================

BG_OUTER = "#0b1220"        # outer canvas
BG_CARD = "#111c2e"         # card surface
BG_CARD_ALT = "#0f1828"     # subtle striping for table rows
BG_PILL = "#1a2740"         # solid pill bg replacing rgba(...,0.12)
BG_BAR_TRACK = "#1e293b"    # progress bar / divider track
BORDER = "#26334a"

TEXT_PRIMARY = "#f1f5f9"
TEXT_SECONDARY = "#cbd5e1"
TEXT_MUTED = "#94a3b8"
TEXT_FAINT = "#64748b"

ACCENT = "#67e8f9"           # cyan
ACCENT_VIOLET = "#a78bfa"
GOOD = "#34d399"
BAD = "#fb7185"
WARN = "#fbbf24"


def _sparkline_svg(values, *, width, height, color):
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = hi - lo if hi > lo else 1.0
    n = len(values)
    step = width / (n - 1) if n > 1 else width
    points = " ".join(
        f"{i*step:.1f},{(height-3) - ((v-lo)/rng)*(height-6):.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:block">'
        f'<polyline points="{points}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


# ============================================================================
# Fortress-dark email — Style 2 (Dashboard Card)
# ============================================================================

def render_email() -> str:
    d = DATA
    pnl_color = GOOD if d["equity_pnl"] >= 0 else BAD
    sign = "+" if d["equity_pnl"] >= 0 else ""

    # Sparklines
    spark_hero = _sparkline_svg(d["equity_30d"], width=520, height=60, color=pnl_color)

    # ---- Helpers (each helper sets bgcolor + inline-bg + !important) ----

    def card(inner_html, *, top_rail_color=None):
        rail = (
            f'<tr><td bgcolor="{top_rail_color}" '
            f'style="background-color:{top_rail_color} !important;'
            f'height:3px;line-height:3px;font-size:0">&nbsp;</td></tr>'
        ) if top_rail_color else ""
        return (
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0" width="100%" bgcolor="{BG_CARD}" '
            f'style="background-color:{BG_CARD} !important;'
            f'border:1px solid {BORDER};border-radius:14px;'
            f'border-collapse:separate">'
            f'{rail}<tr><td bgcolor="{BG_CARD}" '
            f'style="background-color:{BG_CARD} !important;padding:0">'
            f'{inner_html}</td></tr></table>'
        )

    def row(*tds):
        cells = "".join(tds)
        return (
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0" width="100%" bgcolor="{BG_OUTER}" '
            f'style="background-color:{BG_OUTER} !important">'
            f'<tr>{cells}</tr></table>'
        )

    # ---- Brand gradient bar ----
    brand_bar = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr><td height="3" bgcolor="{ACCENT}" '
        f'style="background-color:{ACCENT} !important;'
        f'background:linear-gradient(90deg,{ACCENT} 0%,{ACCENT_VIOLET} 50%,{BAD} 100%) !important;'
        f'height:3px;line-height:3px;font-size:0">&nbsp;</td></tr></table>'
    )

    # ---- Header ----
    header = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;padding:28px 28px 8px">'
        # Solid pill — no rgba so Gmail can't invert it
        f'<table cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="{BG_PILL}" '
        f'style="background-color:{BG_PILL} !important;'
        f'padding:5px 11px;border-radius:6px">'
        f'<font color="{ACCENT}" style="color:{ACCENT}">'
        f'<span style="color:{ACCENT};font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif;'
        f'font-size:11px;letter-spacing:1.4px;text-transform:uppercase;font-weight:600">'
        f'Daily Digest &middot; {d["date_short"]}</span></font>'
        f'</td></tr></table>'
        f'<h1 style="margin:14px 0 4px;color:{TEXT_PRIMARY};'
        f'font-family:Inter,-apple-system,sans-serif;font-size:30px;'
        f'font-weight:700;letter-spacing:-0.025em;line-height:1.15">'
        f'<font color="{TEXT_PRIMARY}">{d["date_long"]}</font></h1>'
        f'<p style="margin:0;color:{TEXT_MUTED};font-size:14px;'
        f'font-family:Inter,sans-serif">'
        f'<font color="{TEXT_MUTED}">Session closed at {d["timestamp"]} &middot; regime '
        f'<span style="color:{ACCENT}"><font color="{ACCENT}">{d["regime"]}</font></span>'
        f'</font></p>'
        f'</td></tr></table>'
    )

    # ---- Hero KPI card ----
    hero_inner = (
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{BG_CARD}" style="background-color:{BG_CARD} !important">'
        f'<tr><td bgcolor="{BG_CARD}" style="background-color:{BG_CARD} !important;padding:24px">'
        f'<font color="{TEXT_MUTED}">'
        f'<span style="color:{TEXT_MUTED};font-family:Inter,sans-serif;font-size:11px;'
        f'letter-spacing:1.4px;text-transform:uppercase;font-weight:600">'
        f'Today’s P&amp;L</span></font>'
        f'<div style="margin-top:10px;font-family:Inter,sans-serif;'
        f'color:{pnl_color};font-size:48px;font-weight:700;'
        f'letter-spacing:-0.03em;line-height:1">'
        f'<font color="{pnl_color}">{sign}${d["equity_pnl"]:.2f}</font></div>'
        f'<div style="margin-top:8px;color:{pnl_color};font-size:15px;'
        f'font-weight:600;font-family:Inter,sans-serif">'
        f'<font color="{pnl_color}">{sign}{d["equity_pnl_pct"]:.2f}% &middot; '
        f'${d["equity_close"]:,.2f}</font>'
        f'<font color="{TEXT_MUTED}">'
        f'<span style="color:{TEXT_MUTED};margin-left:8px;font-weight:400">'
        f'(${d["equity_open"]:,.0f} → ${d["equity_close"]:,.0f})'
        f'</span></font></div>'
        f'<div style="margin-top:18px">{spark_hero}</div>'
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="margin-top:6px"><tr>'
        f'<td bgcolor="{BG_CARD}" style="background-color:{BG_CARD} !important;'
        f'color:{TEXT_FAINT};font-size:11px;font-family:Inter,sans-serif;text-align:left">'
        f'<font color="{TEXT_FAINT}">30 sessions ago</font></td>'
        f'<td bgcolor="{BG_CARD}" style="background-color:{BG_CARD} !important;'
        f'color:{TEXT_FAINT};font-size:11px;font-family:Inter,sans-serif;text-align:right">'
        f'<font color="{TEXT_FAINT}">Today</font></td>'
        f'</tr></table>'
        f'</td></tr></table>'
    )

    hero_block = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;padding:18px 16px 0">'
        f'{card(hero_inner)}'
        f'</td></tr></table>'
    )

    # ---- KPI grid (4 mini cards) ----
    def mini_card(label, value, sub, accent):
        return (
            f'<td valign="top" bgcolor="{BG_OUTER}" width="25%" '
            f'style="background-color:{BG_OUTER} !important;padding:0 4px">'
            f'<table cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'bgcolor="{BG_CARD}" '
            f'style="background-color:{BG_CARD} !important;'
            f'border:1px solid {BORDER};border-radius:12px">'
            f'<tr><td bgcolor="{BG_CARD}" '
            f'style="background-color:{BG_CARD} !important;padding:14px">'
            f'<font color="{TEXT_MUTED}">'
            f'<span style="color:{TEXT_MUTED};font-family:Inter,sans-serif;'
            f'font-size:10px;letter-spacing:1.3px;text-transform:uppercase;'
            f'font-weight:600">{label}</span></font>'
            f'<div style="margin-top:8px;color:{accent};font-size:22px;'
            f'font-weight:700;letter-spacing:-0.02em;font-family:Inter,sans-serif">'
            f'<font color="{accent}">{value}</font></div>'
            f'<div style="margin-top:4px;color:{TEXT_SECONDARY};font-size:11px;'
            f'font-family:Inter,sans-serif">'
            f'<font color="{TEXT_SECONDARY}">{sub}</font></div>'
            f'</td></tr></table></td>'
        )

    kpi_grid = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;padding:12px 12px 0">'
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr>'
        + mini_card("Realized", f"${d['realized_pnl']:.2f}", "today only", GOOD)
        + mini_card("Unrealized", f"${d['unrealized_pnl']:.2f}", "open positions", ACCENT)
        + mini_card("Trades", str(d["trades_count"]),
                    f"{d['trades_buys']} buys / {d['trades_count']-d['trades_buys']} exit", TEXT_PRIMARY)
        + mini_card("Win rate &middot; 7d", f"{d['win_rate_7d']*100:.0f}%", "5 of 7 closed", ACCENT_VIOLET)
        + f'</tr></table>'
        f'</td></tr></table>'
    )

    # ---- Session review (3-column cards) ----
    def review_card(title, items, color, glyph):
        bullets = "".join(
            f"<li style='margin:6px 0;color:{TEXT_PRIMARY};font-size:13px;"
            f"line-height:1.55;font-family:Inter,sans-serif'>"
            f"<font color='{TEXT_PRIMARY}'>{i}</font></li>"
            for i in (items or ["—"])
        )
        return (
            f"<td valign='top' bgcolor='{BG_OUTER}' width='33%' "
            f"style='background-color:{BG_OUTER} !important;padding:0 4px'>"
            f"<table cellpadding='0' cellspacing='0' border='0' width='100%' "
            f"bgcolor='{BG_CARD}' "
            f"style='background-color:{BG_CARD} !important;"
            f"border:1px solid {BORDER};border-radius:12px;border-collapse:separate'>"
            f"<tr><td bgcolor='{color}' "
            f"style='background-color:{color} !important;height:3px;line-height:3px;"
            f"font-size:0'>&nbsp;</td></tr>"
            f"<tr><td bgcolor='{BG_CARD}' "
            f"style='background-color:{BG_CARD} !important;padding:14px 16px'>"
            f"<font color='{color}'>"
            f"<span style='color:{color};font-family:Inter,sans-serif;font-size:11px;"
            f"letter-spacing:1.3px;text-transform:uppercase;font-weight:700'>"
            f"{glyph} {title}</span></font>"
            f"<ul style='margin:10px 0 0;padding-left:18px'>{bullets}</ul>"
            f"</td></tr></table></td>"
        )

    review_block = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;padding:24px 12px 0">'
        f'<font color="{TEXT_MUTED}">'
        f'<div style="color:{TEXT_MUTED};font-family:Inter,sans-serif;font-size:11px;'
        f'letter-spacing:1.4px;text-transform:uppercase;font-weight:600;margin:0 4px 10px">'
        f'Session Review</div></font>'
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{BG_OUTER}" style="background-color:{BG_OUTER} !important">'
        f'<tr>'
        + review_card("Went Well", d["session_review"]["well"], GOOD, "✓")
        + review_card("Went Wrong", d["session_review"]["wrong"], BAD, "✗")
        + review_card("Could Be Better", d["session_review"]["improve"], WARN, "→")
        + f'</tr></table></td></tr></table>'
    )

    # ---- Trades card ----
    def trade_row(t, *, last):
        side_color = GOOD if t["side"] == "BUY" else BAD
        side_bg = "#0f2a1f" if t["side"] == "BUY" else "#2a0f17"
        border_top = f"border-top:1px solid {BG_BAR_TRACK};" if not last else ""
        return (
            f"<tr>"
            f"<td bgcolor='{BG_CARD}' "
            f"style='background-color:{BG_CARD} !important;"
            f"padding:12px 16px;color:{TEXT_MUTED};font-size:13px;"
            f"font-family:Inter,sans-serif;{border_top}'>"
            f"<font color='{TEXT_MUTED}'>{t['time']}</font></td>"
            f"<td bgcolor='{BG_CARD}' "
            f"style='background-color:{BG_CARD} !important;padding:12px 8px;{border_top}'>"
            f"<table cellpadding='0' cellspacing='0' border='0'><tr>"
            f"<td bgcolor='{side_bg}' "
            f"style='background-color:{side_bg} !important;"
            f"padding:3px 9px;border-radius:5px'>"
            f"<font color='{side_color}'>"
            f"<span style='color:{side_color};font-size:11px;font-weight:700;"
            f"font-family:Inter,sans-serif'>{t['side']}</span></font>"
            f"</td></tr></table></td>"
            f"<td bgcolor='{BG_CARD}' "
            f"style='background-color:{BG_CARD} !important;"
            f"padding:12px 12px;color:{TEXT_PRIMARY};font-size:14px;font-weight:600;"
            f"font-family:Inter,sans-serif;{border_top}'>"
            f"<font color='{TEXT_PRIMARY}'>{t['symbol']}</font></td>"
            f"<td bgcolor='{BG_CARD}' align='right' "
            f"style='background-color:{BG_CARD} !important;"
            f"padding:12px 16px;color:{TEXT_SECONDARY};font-size:13px;text-align:right;"
            f"font-family:&apos;SF Mono&apos;,Menlo,monospace;{border_top}'>"
            f"<font color='{TEXT_SECONDARY}'>{t['qty']} × ${t['price']}</font></td>"
            f"<td bgcolor='{BG_CARD}' align='right' "
            f"style='background-color:{BG_CARD} !important;"
            f"padding:12px 16px;color:{TEXT_MUTED};font-size:12px;text-align:right;"
            f"font-family:Inter,sans-serif;{border_top}'>"
            f"<font color='{TEXT_MUTED}'>{t['outcome']}</font></td>"
            f"</tr>"
        )

    trades_inner = (
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{BG_CARD}" '
        f'style="background-color:{BG_CARD} !important;border-collapse:collapse">'
        f'<tr><td bgcolor="{BG_CARD}" '
        f'style="background-color:{BG_CARD} !important;padding:16px 18px 8px">'
        f'<font color="{ACCENT}">'
        f'<span style="color:{ACCENT};font-family:Inter,sans-serif;font-size:11px;'
        f'letter-spacing:1.3px;text-transform:uppercase;font-weight:700">'
        f'Today’s Trades &middot; {d["trades_count"]}</span></font>'
        f'</td></tr>'
        f'<tr><td bgcolor="{BG_CARD}" '
        f'style="background-color:{BG_CARD} !important">'
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'bgcolor="{BG_CARD}" '
        f'style="background-color:{BG_CARD} !important;border-collapse:collapse">'
        + "".join(trade_row(t, last=(i == 0)) for i, t in enumerate(d["trades"]))
        + f'</table></td></tr></table>'
    )

    trades_block = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;padding:18px 16px 0">'
        f'{card(trades_inner)}'
        f'</td></tr></table>'
    )

    # ---- Footer ----
    footer = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;padding:32px 28px 24px;'
        f'border-top:1px solid {BG_BAR_TRACK};color:{TEXT_FAINT};font-size:11px;'
        f'font-family:Inter,sans-serif">'
        f'<font color="{TEXT_FAINT}">phase4-v1 &middot; HEAD &middot; paper trading</font>'
        f'</td></tr></table>'
    )

    # ---- Outer envelope (fortress: bgcolor on body, table, td) ----
    head = (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        '<meta name="color-scheme" content="dark only"/>'
        '<meta name="supported-color-schemes" content="dark"/>'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge"/>'
        f'<title>Daily Digest &middot; {DATA["date_short"]}</title>'
        '<style type="text/css">'
        ':root { color-scheme: dark; supported-color-schemes: dark; } '
        f'body, table, td {{ background-color: {BG_OUTER} !important; }} '
        f'a, a:link, a:visited {{ color: {ACCENT} !important; '
        'text-decoration: none !important; } '
        # Gmail mobile dark-mode signal — locks our palette so Gmail can't
        # invert. Apply to BG and FG independently.
        f'[data-ogsb] body, [data-ogsb] table, [data-ogsb] td '
        f'{{ background-color: {BG_OUTER} !important; }} '
        f'[data-ogsc] body, [data-ogsc] table, [data-ogsc] td '
        f'{{ color: {TEXT_PRIMARY} !important; }} '
        # Apple Mail / iOS Mail honor this for true dark
        '@media (prefers-color-scheme: dark) { '
        f'  body, table, td {{ background-color: {BG_OUTER} !important; }} '
        '} '
        # Force same palette in light-mode UI: every email, dark, no exceptions
        '@media (prefers-color-scheme: light) { '
        f'  body, table, td {{ background-color: {BG_OUTER} !important; }} '
        '}'
        '</style>'
        # Outlook (desktop) MSO conditional — Outlook ignores <style> partially
        '<!--[if mso]><style type="text/css">'
        f'body, table, td {{ background-color: {BG_OUTER} !important; '
        f'color: {TEXT_PRIMARY} !important; }}'
        '</style><![endif]-->'
        '</head>'
    )

    body_open = (
        f'<body bgcolor="{BG_OUTER}" '
        f'style="margin:0;padding:0;background-color:{BG_OUTER} !important;'
        f'color:{TEXT_PRIMARY};'
        f"font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        '-webkit-text-size-adjust:100%;color-scheme:dark;supported-color-schemes:dark">'
    )

    return (
        head + body_open
        + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;margin:0;padding:0">'
        f'<tr><td bgcolor="{BG_OUTER}" align="center" '
        f'style="background-color:{BG_OUTER} !important;padding:16px 8px">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="640" bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important;max-width:640px;width:100%">'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">{brand_bar}</td></tr>'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">{header}</td></tr>'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">{hero_block}</td></tr>'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">{kpi_grid}</td></tr>'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">{review_block}</td></tr>'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">{trades_block}</td></tr>'
        f'<tr><td bgcolor="{BG_OUTER}" '
        f'style="background-color:{BG_OUTER} !important">{footer}</td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def main():
    from dotenv import load_dotenv
    load_dotenv()
    from trading_bot.config import Settings, load_config
    from trading_bot.email_sender import EmailSender

    settings = Settings()
    cfg = load_config(Path("strategy/config.yaml"))
    sender = EmailSender(
        user=settings.gmail_user,
        app_password=settings.gmail_app_password,
        to=cfg.email.to,
    )
    html = render_email()
    Path("/tmp/digest_fortress.html").write_text(html)
    sender.send(
        subject="[FORTRESS-DARK] Dashboard Card — should be dark in Gmail mobile",
        html_body=html,
    )
    print("sent fortress-dark preview")
    print(f"local copy: /tmp/digest_fortress.html ({len(html)} bytes)")


if __name__ == "__main__":
    main()
