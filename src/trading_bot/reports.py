from decimal import Decimal

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.intelligence import IntelligenceBundle
from trading_bot.orchestrator import ScanResult
from trading_bot.portfolio_monitor import Event


def build_daily_report_html(
    *,
    account: AccountSnapshot,
    positions: list[Position],
    scan: ScanResult,
    spy_daily_change_pct: Decimal,
    regime: str,
) -> str:
    pos_rows = "".join(
        f"<tr><td>{p.symbol}</td><td>{p.qty}</td>"
        f"<td>${p.avg_entry_price}</td>"
        f"<td>${p.market_value}</td>"
        f"<td style='color:{'green' if p.unrealized_pl >= 0 else 'red'}'>${p.unrealized_pl}</td></tr>"
        for p in positions
    ) or "<tr><td colspan='5'><i>No open positions</i></td></tr>"

    dec_rows = "".join(
        f"<tr><td>{d.symbol}</td><td>{d.action}</td><td>{d.reason}</td></tr>"
        for d in scan.decisions
    ) or "<tr><td colspan='3'><i>No decisions this run</i></td></tr>"

    return f"""
<h2>Trading Bot — Daily Report</h2>
<p><b>Generated:</b> {scan.timestamp.isoformat(timespec='seconds')}<br>
<b>Regime:</b> {regime}<br>
<b>SPY daily move:</b> {spy_daily_change_pct}%</p>

<h3>Account</h3>
<table border='1' cellpadding='6'>
  <tr><th>Equity</th><td>${account.equity}</td></tr>
  <tr><th>Cash</th><td>${account.cash}</td></tr>
  <tr><th>Buying Power</th><td>${account.buying_power}</td></tr>
  <tr><th>Portfolio Value</th><td>${account.portfolio_value}</td></tr>
</table>

<h3>Open Positions</h3>
<table border='1' cellpadding='6'>
  <tr><th>Symbol</th><th>Qty</th><th>Avg Entry</th><th>Market Value</th><th>Unrealized P&amp;L</th></tr>
  {pos_rows}
</table>

<h3>Decisions This Run</h3>
<table border='1' cellpadding='6'>
  <tr><th>Symbol</th><th>Action</th><th>Reason</th></tr>
  {dec_rows}
</table>
"""


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
) -> str:
    """Comprehensive HTML report including macro, news, regime, decisions, positions."""
    base = build_daily_report_html(
        account=account, positions=positions, scan=scan,
        spy_daily_change_pct=spy_daily_change_pct, regime=regime,
    )

    # Macro section
    m = intel.macro
    macro_html = f"""
<h3>Macro Snapshot ({m.fetched_at.isoformat(timespec='minutes')})</h3>
<table border='1' cellpadding='6'>
  <tr><th>VIX (FRED)</th><td>{m.vix if m.vix is not None else 'n/a'}</td></tr>
  <tr><th>10Y Treasury Yield</th><td>{m.yield_10y_pct if m.yield_10y_pct is not None else 'n/a'}%</td></tr>
  <tr><th>Effective Fed Funds Rate</th><td>{m.fed_funds_pct if m.fed_funds_pct is not None else 'n/a'}%</td></tr>
</table>
"""

    # Per-symbol news section
    news_blocks = []
    for sym, items in intel.news_by_symbol.items():
        if not items:
            continue
        rows = "".join(
            f"<li><a href='{n.url}'>{n.headline}</a> "
            f"<small>({n.published_at.strftime('%H:%M UTC')}, {n.source})</small></li>"
            for n in items
        )
        news_blocks.append(f"<h4>{sym}</h4><ul>{rows}</ul>")
    news_html = "<h3>Per-Symbol News (last 48h)</h3>" + (
        "".join(news_blocks) if news_blocks else "<p><i>No fresh headlines.</i></p>"
    )

    # GDELT macro news
    gdelt_html = ""
    if intel.gdelt:
        rows = "".join(
            f"<li><a href='{e.url}'>{e.title}</a> "
            f"<small>(tone {e.sentiment:+.1f}, {e.sourcecountry})</small></li>"
            for e in intel.gdelt[:6]
        )
        gdelt_html = f"<h3>Global Macro News (GDELT)</h3><ul>{rows}</ul>"

    # Insider activity
    insider_html = ""
    if intel.insider:
        rows = "".join(
            f"<li>{f.company} <small>({f.filed_at[:10]})</small></li>"
            for f in intel.insider[:8]
        )
        insider_html = f"<h3>Recent Insider Filings (Form 4)</h3><ul>{rows}</ul>"

    # Portfolio events
    events_html = ""
    if events:
        rows = "".join(
            f"<li style='color:{'red' if e.severity == 'alert' else 'black'}'>"
            f"<b>[{e.severity.upper()}]</b> {e.kind}: {e.message}</li>"
            for e in events
        )
        events_html = f"<h3>Portfolio Events Since Last Snapshot</h3><ul>{rows}</ul>"

    return f"""<h1>Trading Bot — {period.upper()} Rich Report</h1>
{base}
{macro_html}
{events_html}
{news_html}
{gdelt_html}
{insider_html}
"""


def build_alert_email_html(events: list[Event], account_equity: str) -> str:
    """Compact alert email for portfolio-watch material events."""
    rows = "".join(
        f"<tr><td>[{e.severity.upper()}]</td><td>{e.kind}</td>"
        f"<td>{e.symbol}</td><td>{e.message}</td></tr>"
        for e in events
    )
    return f"""
<h2>Trading Bot — Portfolio Alert</h2>
<p>Equity: ${account_equity}</p>
<table border='1' cellpadding='6'>
  <tr><th>Severity</th><th>Kind</th><th>Symbol</th><th>Message</th></tr>
  {rows}
</table>
"""
