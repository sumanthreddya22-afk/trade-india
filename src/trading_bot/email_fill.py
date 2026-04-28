"""Per-trade fill email builder. Phase 1 version: symbol, qty, fill price,
slippage, strategy, stop, account equity. Phase 3 will enrich with
leaderboard rank + conviction once those exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class FillContext:
    side: str  # "BUY" | "SELL" | "STOP"
    symbol: str
    qty: Decimal
    fill_price: Decimal
    expected_price: Decimal
    strategy: str
    stop_price: Optional[Decimal]
    account_equity: Decimal
    realized_pnl: Optional[Decimal] = None


@dataclass
class Email:
    subject: str
    html_body: str


def _fmt_money(x: Decimal) -> str:
    return f"${x:,.2f}"


def build_fill_email(ctx: FillContext) -> Email:
    slippage = ctx.fill_price - ctx.expected_price
    if ctx.side == "STOP":
        subject = f"STOP HIT {ctx.symbol} {ctx.qty} @ {_fmt_money(ctx.fill_price)}"
    else:
        subject = f"{ctx.side} {ctx.symbol} {ctx.qty} @ {_fmt_money(ctx.fill_price)}"

    body_lines = [
        f"<h2>{subject}</h2>",
        f"<table>",
        f"<tr><td>Symbol</td><td><b>{ctx.symbol}</b></td></tr>",
        f"<tr><td>Qty</td><td>{ctx.qty}</td></tr>",
        f"<tr><td>Fill price</td><td>{_fmt_money(ctx.fill_price)}</td></tr>",
        f"<tr><td>Expected price</td><td>{_fmt_money(ctx.expected_price)}</td></tr>",
        f"<tr><td>Slippage</td><td>{_fmt_money(slippage)}</td></tr>",
        f"<tr><td>Strategy</td><td>{ctx.strategy}</td></tr>",
    ]
    if ctx.stop_price is not None:
        body_lines.append(f"<tr><td>Stop</td><td>{_fmt_money(ctx.stop_price)}</td></tr>")
    if ctx.realized_pnl is not None:
        sign = "-" if ctx.realized_pnl < 0 else ""
        body_lines.append(
            f"<tr><td>Realized P&amp;L</td><td>{sign}{_fmt_money(abs(ctx.realized_pnl))}</td></tr>"
        )
    body_lines.append(
        f"<tr><td>Account equity</td><td>{_fmt_money(ctx.account_equity)}</td></tr>"
    )
    body_lines.append("</table>")

    return Email(subject=subject, html_body="\n".join(body_lines))
