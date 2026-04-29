"""Generate three email-design wireframes and send them via the bot's
existing EmailSender. Each wireframe is a self-contained HTML email
showing the SAME sample data in three different visual styles, so the
operator can pick which to roll out across all email types.

Run:  .venv/bin/python tools/email_wireframes.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ============================================================================
# Sample data — same numbers across all 3 styles for fair comparison
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
    "wheel_open": 0,
    "wheel_collateral_pct": 0.0,
    "wheel_pnl_mtd": 0.0,
    "drawdown_pct": 1.4,
    "drawdown_cap": 20.0,
    "daily_loss_pct": 0.0,
    "weekly_loss_pct": -0.8,
    "trades": [
        {"time": "10:32", "side": "BUY", "symbol": "DELL",
         "qty": "2", "price": "215.97", "stop": "207.32", "outcome": "open"},
        {"time": "11:14", "side": "BUY", "symbol": "MRVL",
         "qty": "8", "price": "62.84", "stop": "60.32", "outcome": "open"},
        {"time": "13:48", "side": "SELL", "symbol": "GOOGL",
         "qty": "3", "price": "172.45", "stop": "—", "outcome": "exit +1.8%"},
    ],
    "positions": [
        {"symbol": "DELL", "qty": 2, "mv": 433.94, "pnl_pct": 0.4},
        {"symbol": "MRVL", "qty": 8, "mv": 502.72, "pnl_pct": 1.2},
        {"symbol": "BTC/USD", "qty": 0.012, "mv": 768.40, "pnl_pct": -0.3},
        {"symbol": "AAPL", "qty": 4, "mv": 856.20, "pnl_pct": 2.1},
    ],
    "watchlist": [
        {"symbol": "NVDA", "px": "143.20", "chg": "+2.4%", "rsi": 67, "note": "earnings 2d"},
        {"symbol": "META", "px": "612.05", "chg": "+1.1%", "rsi": 61, "note": "MACD>signal"},
        {"symbol": "ETH/USD", "px": "2,418.22", "chg": "+3.2%", "rsi": 64, "note": "—"},
        {"symbol": "AMD", "px": "168.40", "chg": "-0.6%", "rsi": 54, "note": "below ema20"},
    ],
    "intel_blocks": [
        ("AAPL", "earnings within 5d (Finnhub)"),
        ("MSFT", "earnings within 5d (Finnhub)"),
    ],
    "errors": [],
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


def _sparkline_svg(values, *, width=120, height=24, color="#22d3ee"):
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
        f'stroke-width="1.5" stroke-linejoin="round"/></svg>'
    )


# ============================================================================
# STYLE 1 — TRADING TERMINAL
# Bloomberg/Reuters aesthetic: dense, monospace, color-coded, info-rich
# ============================================================================

def render_terminal_style() -> str:
    d = DATA
    pnl_color = "#22c55e" if d["equity_pnl"] >= 0 else "#ef4444"
    sign = "+" if d["equity_pnl"] >= 0 else "-"

    spark = _sparkline_svg(d["equity_30d"], width=560, height=40, color=pnl_color)

    # Hero P&L block — the big number
    hero = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="background:#000814;border-bottom:1px solid #1e293b">
      <tr><td style="padding:32px 28px 20px">
        <div style="font-family:'SF Mono',Menlo,monospace;font-size:11px;
                    color:#64748b;letter-spacing:2px;text-transform:uppercase">
          DAILY DIGEST · {d["date_long"].upper()} · {d["timestamp"]}
        </div>
        <div style="margin-top:18px;font-family:'SF Mono',Menlo,monospace;
                    color:{pnl_color};font-size:64px;font-weight:700;
                    line-height:1;letter-spacing:-0.04em">
          {sign}{abs(d["equity_pnl_pct"]):.2f}%
        </div>
        <div style="margin-top:8px;font-family:'SF Mono',Menlo,monospace;
                    color:#cbd5e1;font-size:18px">
          ${d["equity_close"]:,.2f}
          <span style="color:{pnl_color};margin-left:14px">
            {sign}${abs(d["equity_pnl"]):.2f}
          </span>
          <span style="color:#64748b;margin-left:14px">
            from ${d["equity_open"]:,.2f}
          </span>
        </div>
      </td></tr>
      <tr><td style="padding:0 28px 28px">{spark}</td></tr>
    </table>
    """

    # Compact metric strip
    metrics = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="background:#020916;border-bottom:1px solid #1e293b;
                  font-family:'SF Mono',Menlo,monospace">
      <tr>
        <td style="padding:16px 28px;width:25%;border-right:1px solid #1e293b">
          <div style="color:#64748b;font-size:10px;letter-spacing:1.5px;
                      text-transform:uppercase">REALIZED</div>
          <div style="color:{'#22c55e' if d['realized_pnl']>=0 else '#ef4444'};
                      font-size:18px;font-weight:600;margin-top:4px">
            +${d["realized_pnl"]:.2f}
          </div>
        </td>
        <td style="padding:16px 28px;width:25%;border-right:1px solid #1e293b">
          <div style="color:#64748b;font-size:10px;letter-spacing:1.5px;
                      text-transform:uppercase">UNREALIZED</div>
          <div style="color:{'#22c55e' if d['unrealized_pnl']>=0 else '#ef4444'};
                      font-size:18px;font-weight:600;margin-top:4px">
            +${d["unrealized_pnl"]:.2f}
          </div>
        </td>
        <td style="padding:16px 28px;width:25%;border-right:1px solid #1e293b">
          <div style="color:#64748b;font-size:10px;letter-spacing:1.5px;
                      text-transform:uppercase">REGIME</div>
          <div style="color:#22d3ee;font-size:18px;font-weight:600;margin-top:4px">
            {d["regime"].upper()}
          </div>
        </td>
        <td style="padding:16px 28px;width:25%">
          <div style="color:#64748b;font-size:10px;letter-spacing:1.5px;
                      text-transform:uppercase">VIX</div>
          <div style="color:#f1f5f9;font-size:18px;font-weight:600;margin-top:4px">
            {d["vix"]:.2f}
          </div>
        </td>
      </tr>
    </table>
    """

    # Trades table — Bloomberg-style monospace
    trades_rows = "".join(
        f"<tr><td style='padding:10px 16px;color:#64748b'>{t['time']}</td>"
        f"<td style='padding:10px 16px;color:{'#22c55e' if t['side']=='BUY' else '#ef4444'};font-weight:600'>{t['side']}</td>"
        f"<td style='padding:10px 16px;color:#f1f5f9;font-weight:600'>{t['symbol']}</td>"
        f"<td style='padding:10px 16px;color:#cbd5e1;text-align:right'>{t['qty']}</td>"
        f"<td style='padding:10px 16px;color:#cbd5e1;text-align:right'>${t['price']}</td>"
        f"<td style='padding:10px 16px;color:#cbd5e1;text-align:right'>{t['stop']}</td>"
        f"<td style='padding:10px 16px;color:#94a3b8;text-align:right'>{t['outcome']}</td></tr>"
        for t in d["trades"]
    )

    trades_table = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="font-family:'SF Mono',Menlo,monospace;font-size:13px;
                  border-collapse:collapse">
      <thead><tr style="background:#020916">
        <th style="padding:10px 16px;color:#64748b;font-size:10px;
                   letter-spacing:1.5px;text-align:left;text-transform:uppercase;
                   font-weight:600;border-bottom:1px solid #1e293b">TIME</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:left;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">SIDE</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:left;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">SYMBOL</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">QTY</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">PX</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">STOP</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">OUTCOME</th>
      </tr></thead>
      <tbody>{trades_rows}</tbody>
    </table>
    """

    # Watchlist — same density
    wl_rows = "".join(
        f"<tr><td style='padding:8px 16px;color:#f1f5f9;font-weight:600'>{w['symbol']}</td>"
        f"<td style='padding:8px 16px;color:#cbd5e1;text-align:right'>{w['px']}</td>"
        f"<td style='padding:8px 16px;color:{'#22c55e' if w['chg'].startswith('+') else '#ef4444'};text-align:right'>{w['chg']}</td>"
        f"<td style='padding:8px 16px;color:#cbd5e1;text-align:right'>{w['rsi']}</td>"
        f"<td style='padding:8px 16px;color:#94a3b8;text-align:right'>{w['note']}</td></tr>"
        for w in d["watchlist"]
    )

    watchlist_table = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="font-family:'SF Mono',Menlo,monospace;font-size:13px;border-collapse:collapse">
      <thead><tr style="background:#020916">
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:left;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">SYM</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">PX</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">CHG</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">RSI</th>
        <th style="padding:10px 16px;color:#64748b;font-size:10px;letter-spacing:1.5px;
                   text-align:right;text-transform:uppercase;font-weight:600;
                   border-bottom:1px solid #1e293b">NOTE</th>
      </tr></thead><tbody>{wl_rows}</tbody>
    </table>
    """

    # Section header helper
    def _hdr(label):
        return (
            f"<div style='padding:24px 28px 8px;background:#000814'>"
            f"<div style='font-family:\"SF Mono\",Menlo,monospace;color:#22d3ee;"
            f"font-size:11px;letter-spacing:2.5px;text-transform:uppercase;"
            f"font-weight:700;border-bottom:1px solid #1e293b;padding-bottom:8px'>"
            f"&gt;&gt; {label}</div></div>"
        )

    # Session review — inline three blocks
    def _bullets(items, color):
        if not items:
            return f"<div style='color:#64748b;font-size:12px'>(none)</div>"
        return "".join(
            f"<div style='color:#cbd5e1;font-size:12px;line-height:1.6;margin:4px 0;"
            f"font-family:\"SF Mono\",Menlo,monospace'>"
            f"<span style='color:{color}'>▸</span> {i}</div>"
            for i in items
        )

    review = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="background:#000814"><tr>
      <td valign="top" style="width:33%;padding:14px 20px;border-right:1px solid #1e293b">
        <div style="color:#22c55e;font-size:10px;letter-spacing:1.5px;
                    text-transform:uppercase;font-weight:600;margin-bottom:8px;
                    font-family:'SF Mono',Menlo,monospace">+ WENT WELL</div>
        {_bullets(d["session_review"]["well"], "#22c55e")}
      </td>
      <td valign="top" style="width:33%;padding:14px 20px;border-right:1px solid #1e293b">
        <div style="color:#ef4444;font-size:10px;letter-spacing:1.5px;
                    text-transform:uppercase;font-weight:600;margin-bottom:8px;
                    font-family:'SF Mono',Menlo,monospace">- WENT WRONG</div>
        {_bullets(d["session_review"]["wrong"], "#ef4444")}
      </td>
      <td valign="top" style="width:33%;padding:14px 20px">
        <div style="color:#f59e0b;font-size:10px;letter-spacing:1.5px;
                    text-transform:uppercase;font-weight:600;margin-bottom:8px;
                    font-family:'SF Mono',Menlo,monospace">→ IMPROVE</div>
        {_bullets(d["session_review"]["improve"], "#f59e0b")}
      </td>
    </tr></table>
    """

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark only"/>
<meta name="supported-color-schemes" content="dark"/>
<title>Daily Digest · Trading Terminal</title>
<style>
:root {{ color-scheme: dark; }}
body, table, td {{ background: #000814 !important; }}
[data-ogsc] body, [data-ogsc] table, [data-ogsc] td
{{ background: #000814 !important; color: #f1f5f9 !important; }}
@media (prefers-color-scheme: dark) {{ body, table, td {{ background: #000814 !important; }} }}
@media (prefers-color-scheme: light) {{ body, table, td {{ background: #000814 !important; color: #f1f5f9 !important; }} }}
</style></head>
<body style="margin:0;padding:0;background:#000814;color:#f1f5f9;
             font-family:'SF Mono',Menlo,Consolas,monospace;
             color-scheme:dark;-webkit-text-size-adjust:100%">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#000814">
<tr><td align="center"><table cellpadding="0" cellspacing="0" border="0" width="640"
       style="max-width:640px;width:100%;background:#000814">
  <tr><td>{hero}</td></tr>
  <tr><td>{metrics}</td></tr>
  <tr><td>{_hdr("SESSION REVIEW")}</td></tr>
  <tr><td>{review}</td></tr>
  <tr><td>{_hdr("TRADES · 3")}</td></tr>
  <tr><td style="padding:12px 0">{trades_table}</td></tr>
  <tr><td>{_hdr("WATCHLIST · TOP MOVERS")}</td></tr>
  <tr><td style="padding:12px 0">{watchlist_table}</td></tr>
  <tr><td style="padding:24px 28px;border-top:1px solid #1e293b;background:#000814;
                 color:#64748b;font-size:10px;letter-spacing:1.5px;
                 font-family:'SF Mono',Menlo,monospace">
    PHASE4-V1 · HEAD · BHARATH-CLAUDE-TRADE
  </td></tr>
</table></td></tr></table></body></html>"""


# ============================================================================
# STYLE 2 — DASHBOARD CARD
# Linear/Stripe/Vercel aesthetic: rounded cards, sparklines, glassy
# ============================================================================

def render_dashboard_style() -> str:
    d = DATA
    pnl_color = "#34d399" if d["equity_pnl"] >= 0 else "#fb7185"
    sign = "+" if d["equity_pnl"] >= 0 else ""

    eq_spark = _sparkline_svg(d["equity_30d"], width=520, height=60, color=pnl_color)
    eq_spark_small = _sparkline_svg(d["equity_30d"][-14:], width=80, height=24, color="#67e8f9")

    # Top brand bar
    brand = """
    <div style="height:3px;background:linear-gradient(90deg,#67e8f9 0%,#a78bfa 50%,#fb7185 100%)"></div>
    """

    # Header — clean minimal title
    header = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td style="padding:28px 28px 8px">
        <div style="display:inline-block;padding:4px 10px;background:rgba(34,211,238,0.12);
                    border-radius:6px;color:#67e8f9;font-size:11px;letter-spacing:1.5px;
                    text-transform:uppercase;font-weight:600;
                    font-family:Inter,sans-serif">
          Daily Digest · {d["date_short"]}
        </div>
        <h1 style="margin:14px 0 4px;color:#f1f5f9;font-size:32px;font-weight:700;
                   letter-spacing:-0.025em;font-family:Inter,-apple-system,sans-serif">
          {d["date_long"]}
        </h1>
        <p style="margin:0;color:#94a3b8;font-size:14px;
                  font-family:Inter,sans-serif">
          Session closed at {d["timestamp"]} · regime <span style="color:#67e8f9">{d["regime"]}</span>
        </p>
      </td></tr>
    </table>
    """

    # Hero KPI card
    hero = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-top:20px"><tr><td style="padding:0 16px">
      <table cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:#111c2e;border:1px solid #26334a;border-radius:14px">
        <tr><td style="padding:24px">
          <div style="color:#94a3b8;font-size:11px;letter-spacing:1.4px;
                      text-transform:uppercase;font-weight:600;
                      font-family:Inter,sans-serif">Today's P&amp;L</div>
          <div style="margin-top:10px;font-family:Inter,sans-serif;color:{pnl_color};
                      font-size:54px;font-weight:700;letter-spacing:-0.03em;line-height:1">
            {sign}${d["equity_pnl"]:.2f}
          </div>
          <div style="margin-top:6px;color:{pnl_color};font-size:16px;font-weight:600;
                      font-family:Inter,sans-serif">
            {sign}{d["equity_pnl_pct"]:.2f}% · ${d["equity_close"]:,.2f}
            <span style="color:#94a3b8;margin-left:8px;font-weight:400">
              ($ {d["equity_open"]:,.0f} → ${d["equity_close"]:,.0f})
            </span>
          </div>
          <div style="margin-top:18px">{eq_spark}</div>
          <div style="display:flex;justify-content:space-between;margin-top:6px;
                      color:#64748b;font-size:11px;font-family:Inter,sans-serif">
            <span>30d ago</span><span>Today</span>
          </div>
        </td></tr>
      </table>
    </td></tr></table>
    """

    # 4-up KPI grid
    def _mini_card(label, value, sub, accent):
        return (
            f"<td valign='top' style='padding:0 4px;width:25%'>"
            f"<table cellpadding='0' cellspacing='0' border='0' width='100%'"
            f" style='background:#111c2e;border:1px solid #26334a;border-radius:12px'>"
            f"<tr><td style='padding:14px'>"
            f"<div style='color:#94a3b8;font-size:10px;letter-spacing:1.3px;"
            f"text-transform:uppercase;font-weight:600;font-family:Inter,sans-serif'>{label}</div>"
            f"<div style='margin-top:8px;color:{accent};font-size:22px;font-weight:700;"
            f"letter-spacing:-0.02em;font-family:Inter,sans-serif'>{value}</div>"
            f"<div style='margin-top:4px;color:#cbd5e1;font-size:11px;"
            f"font-family:Inter,sans-serif'>{sub}</div>"
            f"</td></tr></table></td>"
        )

    kpis = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-top:12px"><tr><td style="padding:0 12px">
      <table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
        {_mini_card("Realized", f"${d['realized_pnl']:.2f}", "today only", "#34d399")}
        {_mini_card("Unrealized", f"${d['unrealized_pnl']:.2f}", "open positions", "#67e8f9")}
        {_mini_card("Trades", f"{d['trades_count']}", f"{d['trades_buys']} buys / {d['trades_count']-d['trades_buys']} exit", "#f1f5f9")}
        {_mini_card("Win rate · 7d", f"{d['win_rate_7d']*100:.0f}%", "5 of 7 closed", "#a78bfa")}
      </tr></table>
    </td></tr></table>
    """

    # Session review — three rounded cards
    def _review_card(title, items, color, glyph):
        bullets = "".join(
            f"<li style='margin:6px 0;color:#f1f5f9;font-size:13px;"
            f"line-height:1.55;font-family:Inter,sans-serif'>{i}</li>"
            for i in (items or ["—"])
        )
        return (
            f"<td valign='top' style='padding:0 4px;width:33%'>"
            f"<table cellpadding='0' cellspacing='0' border='0' width='100%'"
            f" style='background:#111c2e;border:1px solid #26334a;"
            f"border-top:3px solid {color};border-radius:12px'>"
            f"<tr><td style='padding:14px 16px'>"
            f"<div style='color:{color};font-size:11px;letter-spacing:1.3px;"
            f"text-transform:uppercase;font-weight:700;font-family:Inter,sans-serif'>"
            f"{glyph} {title}</div>"
            f"<ul style='margin:10px 0 0;padding-left:18px'>{bullets}</ul>"
            f"</td></tr></table></td>"
        )

    review_section = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-top:24px"><tr><td style="padding:0 12px">
      <div style="color:#94a3b8;font-size:11px;letter-spacing:1.4px;
                  text-transform:uppercase;font-weight:600;margin:0 4px 10px;
                  font-family:Inter,sans-serif">Session Review</div>
      <table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
        {_review_card("Went Well", d["session_review"]["well"], "#34d399", "✓")}
        {_review_card("Went Wrong", d["session_review"]["wrong"], "#fb7185", "✗")}
        {_review_card("Could Be Better", d["session_review"]["improve"], "#fbbf24", "→")}
      </tr></table>
    </td></tr></table>
    """

    # Trades card
    def _trade_row(t):
        side_color = "#34d399" if t["side"] == "BUY" else "#fb7185"
        side_bg = "rgba(52,211,153,0.14)" if t["side"] == "BUY" else "rgba(251,113,133,0.14)"
        return (
            f"<tr>"
            f"<td style='padding:12px 16px;color:#94a3b8;font-size:13px;"
            f"font-family:Inter,sans-serif;border-top:1px solid #1e293b'>{t['time']}</td>"
            f"<td style='padding:12px 16px;border-top:1px solid #1e293b'>"
            f"<span style='display:inline-block;padding:2px 8px;background:{side_bg};"
            f"color:{side_color};border-radius:4px;font-size:11px;font-weight:600;"
            f"font-family:Inter,sans-serif'>{t['side']}</span></td>"
            f"<td style='padding:12px 16px;color:#f1f5f9;font-size:14px;font-weight:600;"
            f"font-family:Inter,sans-serif;border-top:1px solid #1e293b'>{t['symbol']}</td>"
            f"<td style='padding:12px 16px;color:#cbd5e1;font-size:13px;text-align:right;"
            f"font-family:\"SF Mono\",Menlo,monospace;border-top:1px solid #1e293b'>"
            f"{t['qty']} × ${t['price']}</td>"
            f"<td style='padding:12px 16px;color:#94a3b8;font-size:12px;text-align:right;"
            f"font-family:Inter,sans-serif;border-top:1px solid #1e293b'>{t['outcome']}</td>"
            f"</tr>"
        )

    trades_card = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-top:18px"><tr><td style="padding:0 16px">
      <table cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:#111c2e;border:1px solid #26334a;border-radius:12px;
                    overflow:hidden">
        <tr><td style="padding:16px 18px 8px">
          <div style="color:#67e8f9;font-size:11px;letter-spacing:1.3px;
                      text-transform:uppercase;font-weight:700;
                      font-family:Inter,sans-serif">📊 Today's Trades · {d["trades_count"]}</div>
        </td></tr>
        <tr><td>
          <table cellpadding="0" cellspacing="0" border="0" width="100%"
                 style="border-collapse:collapse">
            {''.join(_trade_row(t) for t in d['trades'])}
          </table>
        </td></tr>
      </table>
    </td></tr></table>
    """

    # Footer
    footer = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-top:32px"><tr><td style="padding:18px 28px;
                                                  border-top:1px solid #1e293b;
                                                  color:#64748b;font-size:11px;
                                                  font-family:Inter,sans-serif">
      phase4-v1 · HEAD · paper trading
    </td></tr></table>
    """

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark only"/>
<meta name="supported-color-schemes" content="dark"/>
<title>Daily Digest · Dashboard Card</title>
<style>
:root {{ color-scheme: dark; }}
body, table, td {{ background: #0b1220 !important; }}
[data-ogsc] body, [data-ogsc] table, [data-ogsc] td
{{ background: #0b1220 !important; color: #f1f5f9 !important; }}
@media (prefers-color-scheme: dark) {{ body, table, td {{ background: #0b1220 !important; }} }}
@media (prefers-color-scheme: light) {{ body, table, td {{ background: #0b1220 !important; color: #f1f5f9 !important; }} }}
</style></head>
<body style="margin:0;padding:0;background:#0b1220;color:#f1f5f9;
             font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             color-scheme:dark;-webkit-text-size-adjust:100%">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0b1220">
<tr><td align="center"><table cellpadding="0" cellspacing="0" border="0" width="640"
       style="max-width:640px;width:100%;background:#0b1220">
  <tr><td>{brand}</td></tr>
  <tr><td>{header}</td></tr>
  <tr><td>{hero}</td></tr>
  <tr><td>{kpis}</td></tr>
  <tr><td>{review_section}</td></tr>
  <tr><td>{trades_card}</td></tr>
  <tr><td>{footer}</td></tr>
</table></td></tr></table></body></html>"""


# ============================================================================
# STYLE 3 — EDITORIAL BRIEFING
# FT/Bloomberg newsletter aesthetic: long-form prose, magazine layout,
# strong typographic hierarchy, big lead, story-driven sections
# ============================================================================

def render_editorial_style() -> str:
    d = DATA
    pnl_color = "#86efac" if d["equity_pnl"] >= 0 else "#fda4af"
    pnl_word = "rose" if d["equity_pnl"] >= 0 else "fell"

    spark = _sparkline_svg(d["equity_30d"], width=520, height=42, color="#86efac")

    masthead = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td style="padding:32px 36px 16px;border-bottom:1px solid #1e293b">
        <div style="font-family:Georgia,'Times New Roman',serif;
                    color:#86efac;font-size:11px;letter-spacing:3.5px;
                    text-transform:uppercase">
          The Trading Brief
        </div>
        <div style="margin-top:4px;color:#94a3b8;font-size:13px;
                    font-family:Georgia,serif;font-style:italic">
          Wednesday — {d["date_long"]}
        </div>
      </td></tr>
    </table>
    """

    # Lead paragraph — synthesis prose
    pnl_text = f"+{d['equity_pnl_pct']:.2f}%" if d["equity_pnl"] >= 0 else f"{d['equity_pnl_pct']:.2f}%"
    review_lead = "; ".join(d["session_review"]["well"][:2])
    improve_lead = d["session_review"]["improve"][0] if d["session_review"]["improve"] else ""

    lead = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td style="padding:32px 36px 0">
        <h1 style="margin:0;color:#f1f5f9;font-family:Georgia,'Times New Roman',serif;
                   font-size:34px;font-weight:400;line-height:1.18;letter-spacing:-0.012em">
          The portfolio {pnl_word} <span style="color:{pnl_color};font-weight:600">{pnl_text}</span>
          today, closing at <span style="font-weight:600">${d["equity_close"]:,.0f}</span>.
        </h1>
        <p style="margin:18px 0 0;color:#cbd5e1;font-size:16px;line-height:1.65;
                  font-family:Georgia,'Times New Roman',serif">
          {review_lead}.
          {improve_lead.lstrip("→ ").rstrip(".")}.
          The market regime remained <em style="color:#86efac">{d["regime"]}</em> with VIX at
          <strong>{d["vix"]:.1f}</strong>.
        </p>
        <div style="margin-top:24px">{spark}</div>
        <div style="display:flex;justify-content:space-between;margin-top:6px;
                    color:#64748b;font-size:11px;font-family:Georgia,serif;
                    font-style:italic">
          <span>30 sessions ago</span><span>Today's close</span>
        </div>
      </td></tr>
    </table>
    """

    # Pull quote — the standout metric
    pull_quote = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-top:32px"><tr>
      <td style="padding:0 36px"><table cellpadding="0" cellspacing="0" border="0"
             width="100%" style="border-left:3px solid #86efac;background:transparent">
        <tr><td style="padding:14px 0 14px 22px">
          <div style="color:#cbd5e1;font-family:Georgia,serif;font-size:22px;
                      line-height:1.4;font-style:italic;font-weight:300">
            "{d["session_review"]["well"][0]}."
          </div>
        </td></tr>
      </table></td>
    </tr></table>
    """

    # Section helper
    def _section(label, body_html):
        return (
            "<table cellpadding='0' cellspacing='0' border='0' width='100%' "
            "style='margin-top:36px'><tr><td style='padding:0 36px'>"
            f"<div style='color:#86efac;font-size:11px;letter-spacing:3.5px;"
            f"text-transform:uppercase;font-family:Georgia,serif;"
            f"border-bottom:1px solid #1e293b;padding-bottom:8px;margin-bottom:14px'>"
            f"{label}</div>{body_html}</td></tr></table>"
        )

    # Numbers — narrative metrics
    nums_html = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="font-family:Georgia,serif"><tr>
      <td style="padding:8px 16px 8px 0;width:50%">
        <div style="color:#94a3b8;font-size:13px;font-style:italic">Realized P&amp;L</div>
        <div style="color:#86efac;font-size:24px;font-weight:600;margin-top:2px">
          +${d["realized_pnl"]:.2f}
        </div>
      </td>
      <td style="padding:8px 0 8px 16px;width:50%">
        <div style="color:#94a3b8;font-size:13px;font-style:italic">Unrealized P&amp;L</div>
        <div style="color:#86efac;font-size:24px;font-weight:600;margin-top:2px">
          +${d["unrealized_pnl"]:.2f}
        </div>
      </td>
    </tr><tr>
      <td style="padding:14px 16px 8px 0;border-top:1px solid #1e293b">
        <div style="color:#94a3b8;font-size:13px;font-style:italic">Trades placed</div>
        <div style="color:#f1f5f9;font-size:24px;font-weight:600;margin-top:2px">
          {d["trades_count"]} <span style="color:#94a3b8;font-size:14px;font-weight:400">
          ({d["trades_buys"]} buys, {d["trades_count"]-d["trades_buys"]} exit)</span>
        </div>
      </td>
      <td style="padding:14px 0 8px 16px;border-top:1px solid #1e293b">
        <div style="color:#94a3b8;font-size:13px;font-style:italic">Win rate · 7 days</div>
        <div style="color:#f1f5f9;font-size:24px;font-weight:600;margin-top:2px">
          {d["win_rate_7d"]*100:.0f}%
        </div>
      </td>
    </tr></table>
    """

    # Trades — narrative form
    def _trade_para(t):
        verb = "purchased" if t["side"] == "BUY" else "exited"
        return (
            f"<p style='margin:14px 0;color:#cbd5e1;font-size:15px;line-height:1.65;"
            f"font-family:Georgia,serif'>"
            f"<span style='color:#94a3b8;font-size:13px;font-style:italic'>{t['time']} —</span> "
            f"<strong style='color:#f1f5f9'>{t['symbol']}</strong>: {verb} "
            f"{t['qty']} share{'s' if t['qty']!='1' else ''} at ${t['price']}"
            f"{', ' + t['outcome'] if t['outcome'] != 'open' else ''}.</p>"
        )

    trades_html = "".join(_trade_para(t) for t in d["trades"])

    # Outlook section — what to watch
    watch_html = "".join(
        f"<p style='margin:10px 0;color:#cbd5e1;font-size:15px;line-height:1.65;"
        f"font-family:Georgia,serif'>"
        f"<strong style='color:#f1f5f9'>{w['symbol']}</strong> at "
        f"<span style='color:{'#86efac' if w['chg'].startswith('+') else '#fda4af'}'>"
        f"${w['px']} ({w['chg']})</span> — RSI {w['rsi']}"
        f"{'; ' + w['note'] if w['note']!='—' else ''}.</p>"
        for w in d["watchlist"][:3]
    )

    colophon = f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-top:48px">
      <tr><td style="padding:24px 36px;border-top:1px solid #1e293b;
                     color:#64748b;font-size:12px;font-family:Georgia,serif;
                     font-style:italic;text-align:center">
        Generated automatically by the trading bot · phase4-v1 · paper account<br>
        <span style="font-size:10px;letter-spacing:1.5px;font-style:normal;
                     text-transform:uppercase;color:#475569;display:inline-block;
                     margin-top:6px">Closing report · {d["timestamp"]}</span>
      </td></tr>
    </table>
    """

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark only"/>
<meta name="supported-color-schemes" content="dark"/>
<title>Daily Digest · Editorial Briefing</title>
<style>
:root {{ color-scheme: dark; }}
body, table, td {{ background: #0a0e1a !important; }}
[data-ogsc] body, [data-ogsc] table, [data-ogsc] td
{{ background: #0a0e1a !important; color: #f1f5f9 !important; }}
@media (prefers-color-scheme: dark) {{ body, table, td {{ background: #0a0e1a !important; }} }}
@media (prefers-color-scheme: light) {{ body, table, td {{ background: #0a0e1a !important; color: #f1f5f9 !important; }} }}
</style></head>
<body style="margin:0;padding:0;background:#0a0e1a;color:#f1f5f9;
             font-family:Georgia,'Times New Roman',serif;
             color-scheme:dark;-webkit-text-size-adjust:100%">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0a0e1a">
<tr><td align="center"><table cellpadding="0" cellspacing="0" border="0" width="640"
       style="max-width:640px;width:100%;background:#0a0e1a">
  <tr><td>{masthead}</td></tr>
  <tr><td>{lead}</td></tr>
  <tr><td>{pull_quote}</td></tr>
  <tr><td>{_section("By the Numbers", nums_html)}</td></tr>
  <tr><td>{_section("Today's Activity", trades_html)}</td></tr>
  <tr><td>{_section("On the Watchlist", watch_html)}</td></tr>
  <tr><td>{colophon}</td></tr>
</table></td></tr></table></body></html>"""


# ============================================================================
# Send each style as a preview email
# ============================================================================

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

    styles = [
        ("[PREVIEW 1/3] Trading Terminal — dense, monospace, Bloomberg-style",
         render_terminal_style()),
        ("[PREVIEW 2/3] Dashboard Card — Linear/Stripe rounded cards",
         render_dashboard_style()),
        ("[PREVIEW 3/3] Editorial Briefing — FT-style newsletter prose",
         render_editorial_style()),
    ]

    for subject, html in styles:
        sender.send(subject=subject, html_body=html)
        print(f"sent: {subject}")


if __name__ == "__main__":
    main()
