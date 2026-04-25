from datetime import datetime
from decimal import Decimal
from pathlib import Path

import click

from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderRequest, OrderSide
from trading_bot.config import Settings, load_config
from trading_bot.email_sender import EmailSender
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.risk_manager import RiskManager, RiskState

CONFIG_PATH = Path("strategy/config.yaml")


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


if __name__ == "__main__":
    main()
