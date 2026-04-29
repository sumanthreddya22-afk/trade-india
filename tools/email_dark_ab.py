"""Three distinct dark-mode strategies to find which one Gmail iOS respects.

Same minimal Style-2 layout in all three; only the bg color + dark-mode
strategy differs. Send all three. User picks the one that renders dark
in Gmail mobile. We then lock in that approach for the email shell.

Strategy A — PURE BLACK (#000000)
   Gmail's auto-invert has a hard floor at true black. Often respected
   when #0b1220 is not.

Strategy B — CHARCOAL (#1f2937 / slate-800)
   Mid-tone warm dark. Gmail's invert algorithm appears to skip darks
   that aren't obviously navy/blue. Used by Apple's iCloud receipts.

Strategy C — LIGHT-FIRST + DARK MEDIA QUERY
   Apple/GitHub/Stripe pattern: design is light-mode-first (so Gmail
   never tries to invert), with `@media (prefers-color-scheme: dark)`
   overrides to look dark when the user's UI is dark. Bulletproof —
   but only "looks dark" when the user's phone is in dark mode.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# Same sample data for all three
D = {
    "date_long": "Wednesday, April 29 2026",
    "date_short": "Apr 29",
    "timestamp": "16:30 ET",
    "equity_close": 14_988.51,
    "equity_open": 14_926.18,
    "equity_pnl": 62.33,
    "equity_pnl_pct": 0.42,
    "realized": 18.50,
    "unrealized": 43.83,
    "trades_count": 3,
    "win_rate_7d": 71,
    "regime": "trending_up",
}


def _hero(*, bg, card_bg, text, text_muted, accent, good, border, font_family,
          dark_meta="dark only"):
    """Reusable hero block. All callers pass their palette + meta in."""
    pnl_color = good if D["equity_pnl"] >= 0 else "#fb7185"
    sign = "+" if D["equity_pnl"] >= 0 else ""

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="{dark_meta}"/>
<meta name="supported-color-schemes" content="dark light"/>
<title>Daily Digest</title>
<style type="text/css">
  body, table, td {{ background-color: {bg} !important; margin: 0; padding: 0; }}
  body {{ font-family: {font_family}; color: {text}; }}
  [data-ogsb] body, [data-ogsb] table, [data-ogsb] td {{ background-color: {bg} !important; }}
  [data-ogsc] * {{ color: {text} !important; }}
  @media (prefers-color-scheme: dark) {{
    body, table, td {{ background-color: {bg} !important; color: {text} !important; }}
  }}
</style>
</head>
<body bgcolor="{bg}" style="margin:0;padding:0;background-color:{bg};color:{text};font-family:{font_family};color-scheme:dark">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{bg}" style="background-color:{bg}!important">
<tr><td align="center" bgcolor="{bg}" style="background-color:{bg}!important;padding:20px 8px">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" bgcolor="{bg}" style="background-color:{bg}!important;max-width:600px;width:100%">

<!-- Header -->
<tr><td bgcolor="{bg}" style="background-color:{bg}!important;padding:0 20px 16px">
  <font color="{text_muted}"><span style="color:{text_muted};font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700">Daily Digest &middot; {D["date_short"]}</span></font>
  <h1 style="margin:8px 0 4px;color:{text};font-size:26px;font-weight:700;letter-spacing:-0.02em;line-height:1.2">
    <font color="{text}">{D["date_long"]}</font>
  </h1>
  <font color="{text_muted}"><span style="color:{text_muted};font-size:13px">Session closed at {D["timestamp"]} &middot; <font color="{accent}"><span style="color:{accent}">{D["regime"]}</span></font></span></font>
</td></tr>

<!-- Hero P&L card -->
<tr><td bgcolor="{bg}" style="background-color:{bg}!important;padding:0 20px">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{card_bg}" style="background-color:{card_bg}!important;border:1px solid {border};border-radius:12px">
    <tr><td bgcolor="{card_bg}" style="background-color:{card_bg}!important;padding:20px">
      <font color="{text_muted}"><span style="color:{text_muted};font-size:11px;letter-spacing:1.4px;text-transform:uppercase;font-weight:700">Today's P&amp;L</span></font>
      <div style="margin-top:8px;color:{pnl_color};font-size:42px;font-weight:700;letter-spacing:-0.03em;line-height:1">
        <font color="{pnl_color}">{sign}${D["equity_pnl"]:.2f}</font>
      </div>
      <div style="margin-top:6px;color:{pnl_color};font-size:14px;font-weight:600">
        <font color="{pnl_color}">{sign}{D["equity_pnl_pct"]:.2f}% &middot; ${D["equity_close"]:,.2f}</font>
        <font color="{text_muted}"> <span style="color:{text_muted};font-weight:400">(${D["equity_open"]:,.0f} → ${D["equity_close"]:,.0f})</span></font>
      </div>
    </td></tr>
  </table>
</td></tr>

<!-- KPI grid -->
<tr><td bgcolor="{bg}" style="background-color:{bg}!important;padding:12px 16px 0">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" bgcolor="{bg}" style="background-color:{bg}!important">
    <tr>
      {_mini_card("Realized", f"${D['realized']:.2f}", good, card_bg, text, text_muted, border)}
      {_mini_card("Unrealized", f"${D['unrealized']:.2f}", accent, card_bg, text, text_muted, border)}
      {_mini_card("Trades", str(D["trades_count"]), text, card_bg, text, text_muted, border)}
      {_mini_card("Win 7d", f"{D['win_rate_7d']}%", "#a78bfa", card_bg, text, text_muted, border)}
    </tr>
  </table>
</td></tr>

<!-- Footer -->
<tr><td bgcolor="{bg}" style="background-color:{bg}!important;padding:24px 20px;border-top:1px solid {border}">
  <font color="{text_muted}"><span style="color:{text_muted};font-size:11px">phase4-v1 &middot; HEAD &middot; paper trading</span></font>
</td></tr>

</table>
</td></tr>
</table>
</body></html>"""


def _mini_card(label, value, accent, card_bg, text, text_muted, border):
    return f"""<td valign="top" width="25%" bgcolor="{card_bg}" style="background-color:{card_bg}!important;padding:12px;border:1px solid {border};border-radius:10px">
      <font color="{text_muted}"><div style="color:{text_muted};font-size:10px;letter-spacing:1.2px;text-transform:uppercase;font-weight:700">{label}</div></font>
      <div style="margin-top:6px;color:{accent};font-size:18px;font-weight:700"><font color="{accent}">{value}</font></div>
    </td>"""


def render_a_pure_black():
    """Strategy A: pure black. Many email clients leave #000 alone."""
    return _hero(
        bg="#000000",
        card_bg="#0a0a0a",
        text="#ffffff",
        text_muted="#a3a3a3",
        accent="#06b6d4",
        good="#22c55e",
        border="#262626",
        font_family="-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif",
    )


def render_b_charcoal():
    """Strategy B: warm charcoal. Less obvious 'navy dark' that triggers
    Gmail's invert. Background is slate-800 #1f2937."""
    return _hero(
        bg="#1f2937",
        card_bg="#111827",
        text="#f9fafb",
        text_muted="#9ca3af",
        accent="#22d3ee",
        good="#34d399",
        border="#374151",
        font_family="-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif",
    )


def render_c_light_first():
    """Strategy C: light-mode-first base, dark via prefers-color-scheme media query.
    Gmail can't invert what's already light. Phone-dark-mode users see the
    dark variant via the media query."""
    bg_light = "#f8fafc"
    card_light = "#ffffff"
    text_light = "#0f172a"
    text_muted_light = "#64748b"
    border_light = "#e2e8f0"
    accent = "#0891b2"
    good = "#16a34a"
    pnl_color = good
    sign = "+"

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="light dark"/>
<meta name="supported-color-schemes" content="light dark"/>
<title>Daily Digest</title>
<style type="text/css">
  body, table, td {{ margin: 0; padding: 0; }}
  body {{ background-color: {bg_light}; color: {text_light};
         font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }}
  .container {{ background-color: {bg_light} !important; }}
  .card {{ background-color: {card_light} !important; }}
  .text-primary {{ color: {text_light} !important; }}
  .text-muted {{ color: {text_muted_light} !important; }}
  .border {{ border-color: {border_light} !important; }}

  /* Apple Mail / iOS Mail / dark-aware Gmail */
  @media (prefers-color-scheme: dark) {{
    body {{ background-color: #0b1220 !important; color: #f1f5f9 !important; }}
    .container {{ background-color: #0b1220 !important; }}
    .card {{ background-color: #111c2e !important; }}
    .text-primary {{ color: #f1f5f9 !important; }}
    .text-muted {{ color: #94a3b8 !important; }}
    .border {{ border-color: #26334a !important; }}
  }}
  /* Gmail mobile dark-mode signal */
  [data-ogsb] body, [data-ogsb] .container {{ background-color: #0b1220 !important; }}
  [data-ogsb] .card {{ background-color: #111c2e !important; }}
  [data-ogsc] .text-primary {{ color: #f1f5f9 !important; }}
  [data-ogsc] .text-muted {{ color: #94a3b8 !important; }}
</style>
</head>
<body class="container" bgcolor="{bg_light}" style="background-color:{bg_light};color:{text_light}">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" class="container" bgcolor="{bg_light}" style="background-color:{bg_light}">
<tr><td align="center" class="container" bgcolor="{bg_light}" style="padding:20px 8px">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%">

<tr><td class="container" style="padding:0 20px 16px">
  <span class="text-muted" style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700">Daily Digest &middot; {D["date_short"]}</span>
  <h1 class="text-primary" style="margin:8px 0 4px;font-size:26px;font-weight:700;letter-spacing:-0.02em;line-height:1.2">{D["date_long"]}</h1>
  <span class="text-muted" style="font-size:13px">Session closed at {D["timestamp"]} &middot; <span style="color:{accent}">{D["regime"]}</span></span>
</td></tr>

<tr><td class="container" style="padding:0 20px">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" class="card border" bgcolor="{card_light}" style="background-color:{card_light};border:1px solid {border_light};border-radius:12px">
    <tr><td class="card" style="padding:20px">
      <span class="text-muted" style="font-size:11px;letter-spacing:1.4px;text-transform:uppercase;font-weight:700">Today's P&amp;L</span>
      <div style="margin-top:8px;color:{pnl_color};font-size:42px;font-weight:700;letter-spacing:-0.03em;line-height:1">{sign}${D["equity_pnl"]:.2f}</div>
      <div style="margin-top:6px;color:{pnl_color};font-size:14px;font-weight:600">{sign}{D["equity_pnl_pct"]:.2f}% &middot; ${D["equity_close"]:,.2f}
        <span class="text-muted" style="font-weight:400">(${D["equity_open"]:,.0f} → ${D["equity_close"]:,.0f})</span></div>
    </td></tr>
  </table>
</td></tr>

<tr><td class="container" style="padding:12px 16px 0">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
    <td valign="top" width="25%" class="card border" bgcolor="{card_light}" style="background-color:{card_light};padding:12px;border:1px solid {border_light};border-radius:10px">
      <span class="text-muted" style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;font-weight:700">Realized</span>
      <div style="margin-top:6px;color:{good};font-size:18px;font-weight:700">${D["realized"]:.2f}</div>
    </td>
    <td valign="top" width="25%" class="card border" bgcolor="{card_light}" style="background-color:{card_light};padding:12px;border:1px solid {border_light};border-radius:10px">
      <span class="text-muted" style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;font-weight:700">Unrealized</span>
      <div style="margin-top:6px;color:{accent};font-size:18px;font-weight:700">${D["unrealized"]:.2f}</div>
    </td>
    <td valign="top" width="25%" class="card border" bgcolor="{card_light}" style="background-color:{card_light};padding:12px;border:1px solid {border_light};border-radius:10px">
      <span class="text-muted" style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;font-weight:700">Trades</span>
      <div class="text-primary" style="margin-top:6px;font-size:18px;font-weight:700">{D["trades_count"]}</div>
    </td>
    <td valign="top" width="25%" class="card border" bgcolor="{card_light}" style="background-color:{card_light};padding:12px;border:1px solid {border_light};border-radius:10px">
      <span class="text-muted" style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;font-weight:700">Win 7d</span>
      <div style="margin-top:6px;color:#a78bfa;font-size:18px;font-weight:700">{D["win_rate_7d"]}%</div>
    </td>
  </tr></table>
</td></tr>

<tr><td class="container border" style="padding:24px 20px;border-top:1px solid {border_light}">
  <span class="text-muted" style="font-size:11px">phase4-v1 &middot; HEAD &middot; paper trading</span>
</td></tr>

</table>
</td></tr>
</table>
</body></html>"""


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

    tests = [
        ("[DARK-TEST A] Pure black #000000 — true black floor",
         render_a_pure_black()),
        ("[DARK-TEST B] Charcoal #1f2937 — warm mid-dark",
         render_b_charcoal()),
        ("[DARK-TEST C] Light-first + dark media query — Apple's pattern",
         render_c_light_first()),
    ]
    for subj, html in tests:
        sender.send(subject=subj, html_body=html)
        print(f"sent: {subj}")


if __name__ == "__main__":
    main()
