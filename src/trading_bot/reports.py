from decimal import Decimal

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.orchestrator import ScanResult


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
