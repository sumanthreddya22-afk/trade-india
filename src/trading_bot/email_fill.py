"""Per-trade fill email builder. Phase 1 version: symbol, qty, fill price,
slippage, strategy, stop, account equity. Phase 3 will enrich with
leaderboard rank + conviction once those exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


_OPTION_FILL_TYPES = {
    "option_csp_open", "option_csp_close",
    "option_cc_open", "option_cc_close",
    "option_roll", "option_assignment", "option_called_away",
}


@dataclass
class FillContext:
    side: str  # "BUY" | "SELL" | "STOP" | option_* (Phase 5)
    symbol: str
    qty: Decimal
    fill_price: Decimal
    expected_price: Decimal
    strategy: str
    stop_price: Optional[Decimal]
    account_equity: Decimal
    realized_pnl: Optional[Decimal] = None
    # Phase 5: option fill metadata (only used when side starts with "option_")
    contract: Optional[str] = None
    strike: Optional[Decimal] = None
    expiration: Optional[str] = None
    premium: Optional[Decimal] = None
    notes: Optional[str] = None


@dataclass
class Email:
    subject: str
    html_body: str


def _fmt_money(x: Decimal) -> str:
    return f"${x:,.2f}"


_OPTION_SUBJECT_LABEL = {
    "option_csp_open": "CSP Opened",
    "option_csp_close": "CSP Closed",
    "option_cc_open": "CC Opened",
    "option_cc_close": "CC Closed",
    "option_roll": "Wheel Roll",
    "option_assignment": "Assigned (CSP)",
    "option_called_away": "Called Away (CC)",
}


def _build_option_fill_email(ctx: FillContext) -> Email:
    """Phase 5 option fill rendering — small inline table with contract details.
    Reuses email_shell helpers (section, data_table, render_shell)."""
    from trading_bot.email_shell import section, data_table, render_shell
    label = _OPTION_SUBJECT_LABEL.get(ctx.side, ctx.side)
    subject = (
        f"{label} {ctx.symbol} "
        f"{ctx.qty} @ {_fmt_money(ctx.fill_price)}"
    )
    rows: list[list[str]] = [
        ["Symbol", str(ctx.symbol)],
        ["Side", ctx.side],
        ["Contract", str(ctx.contract or "—")],
        ["Qty", str(ctx.qty)],
        ["Strike", str(ctx.strike) if ctx.strike is not None else "—"],
        ["Expiration", str(ctx.expiration or "—")],
        ["Premium", _fmt_money(ctx.premium) if ctx.premium is not None
                    else _fmt_money(ctx.fill_price)],
        ["Strategy", ctx.strategy],
    ]
    if ctx.realized_pnl is not None:
        rows.append(["Realized P&L", _fmt_money(ctx.realized_pnl)])
    if ctx.notes:
        rows.append(["Notes", str(ctx.notes)])
    rows.append(["Account equity", _fmt_money(ctx.account_equity)])

    body_html = render_shell(
        title=label,
        status="ok",
        timestamp_et="",
        body_sections=[section(
            title=label, glyph="◆",
            body=data_table(headers=["Field", "Value"], rows=rows),
        )],
    )
    return Email(subject=subject, html_body=body_html)


def build_fill_email(ctx: FillContext) -> Email:
    if ctx.side in _OPTION_FILL_TYPES:
        return _build_option_fill_email(ctx)
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
