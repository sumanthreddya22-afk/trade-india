from datetime import datetime
from decimal import Decimal
from pathlib import Path

import click

from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderRequest, OrderSide
from trading_bot.config import Settings, load_config
from trading_bot.email_sender import EmailSender
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.market_data import MarketDataClient
from trading_bot.orchestrator import ScanResult, TradeOrchestrator
from trading_bot.reports import build_daily_report_html
from trading_bot.risk_manager import RiskManager, RiskState
from trading_bot.state import load_watchlist
from trading_bot.trade_journal import TradeJournal

CONFIG_PATH = Path("strategy/config.yaml")
WATCHLIST_PATH = Path("strategy/watchlist.yaml")


def _build_risk_state() -> RiskState:
    """Stub state — Plan 2 wires this to live P&L calculation."""
    return RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=False,
    )


@click.group()
def main() -> None:
    """Trading bot CLI."""


@main.command()
def status() -> None:
    """Email a snapshot of the current paper account state."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    client = AlpacaClient(settings)
    account = client.get_account()
    positions = client.get_positions()

    rows = "".join(
        f"<tr><td>{p.symbol}</td><td>{p.qty}</td><td>${p.market_value}</td>"
        f"<td>${p.unrealized_pl}</td></tr>"
        for p in positions
    ) or "<tr><td colspan='4'><i>No open positions</i></td></tr>"

    html = f"""
<h2>Trading Bot — Account Status</h2>
<p>Generated {datetime.now().isoformat(timespec='seconds')}</p>
<table border='1' cellpadding='6'>
  <tr><th>Equity</th><td>${account.equity}</td></tr>
  <tr><th>Cash</th><td>${account.cash}</td></tr>
  <tr><th>Buying Power</th><td>${account.buying_power}</td></tr>
  <tr><th>Portfolio Value</th><td>${account.portfolio_value}</td></tr>
</table>
<h3>Open Positions</h3>
<table border='1' cellpadding='6'>
  <tr><th>Symbol</th><th>Qty</th><th>Market Value</th><th>Unrealized P&amp;L</th></tr>
  {rows}
</table>
"""

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    sender.send(subject="Trading Bot — Status", html_body=html)
    click.echo(f"Sent status email to {cfg.email.to}")


@main.command("dry-run")
@click.option("--symbol", required=True)
@click.option("--side", type=click.Choice(["buy", "sell"]), required=True)
@click.option("--qty", required=True, type=str)
@click.option("--price", required=True, type=str)
@click.option("--stop", required=True, type=str)
@click.option(
    "--asset-class",
    type=click.Choice(["stock", "crypto", "option"]),
    default="stock",
)
@click.option(
    "--regime",
    type=click.Choice(["trending_up", "trending_down", "sideways", "risk_off"]),
    default="trending_up",
)
def dry_run(
    symbol: str, side: str, qty: str, price: str, stop: str, asset_class: str, regime: str
) -> None:
    """Validate a hypothetical order through the risk manager. No order is sent."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    client = AlpacaClient(settings)
    account = client.get_account()
    positions = client.get_positions()
    state = _build_risk_state()

    req = OrderRequest(
        symbol=symbol,
        qty=Decimal(qty),
        side=OrderSide(side),
        asset_class=AssetClass(asset_class),
        limit_price=Decimal(price),
        stop_loss_price=Decimal(stop),
    )
    rm = RiskManager(cfg)
    try:
        rm.check(req, account=account, positions=positions, state=state, regime=regime)
    except RiskRuleViolation as e:
        click.echo(f"REJECTED: {e}")
        raise SystemExit(1)
    click.echo(f"PASS: {symbol} {side} {qty} @ ${price} (stop ${stop}) — would be submitted.")


@main.command()
@click.option(
    "--regime",
    type=click.Choice(["trending_up", "trending_down", "sideways", "risk_off"]),
    default="trending_up",
)
def scan(regime: str) -> None:
    """Scan watchlist and place trades on signals (real paper orders)."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    watchlist = load_watchlist(WATCHLIST_PATH)

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
    )
    result = orch.scan(watchlist=watchlist)
    click.echo(f"Scan complete — {len(result.decisions)} decisions:")
    for d in result.decisions:
        click.echo(f"  {d.symbol}: {d.action} ({d.reason})")


@main.command("daily-report")
def daily_report() -> None:
    """Email the daily P&L summary."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)

    account = alpaca.get_account()
    positions = alpaca.get_positions()

    try:
        bars = market.get_daily_bars("SPY", lookback_days=2)
        if len(bars) >= 2:
            yesterday, today = bars["close"].iloc[-2], bars["close"].iloc[-1]
            spy_change = Decimal(str((today / yesterday - 1.0) * 100)).quantize(Decimal("0.01"))
        else:
            spy_change = Decimal("0.00")
    except Exception:
        spy_change = Decimal("0.00")

    empty_scan = ScanResult(decisions=[], timestamp=datetime.now())
    html = build_daily_report_html(
        account=account, positions=positions, scan=empty_scan,
        spy_daily_change_pct=spy_change, regime="trending_up",
    )

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    sender.send(subject="Trading Bot — Daily Report", html_body=html)
    click.echo(f"Sent daily report to {cfg.email.to}")


if __name__ == "__main__":
    main()
