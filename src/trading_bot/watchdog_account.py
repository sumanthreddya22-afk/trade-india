"""Account Sentinel — independent verification path. Queries Alpaca directly,
updates equity HWM, computes drawdown vs HWM, writes pause.flag if breached.
Does NOT trust the daemon's view of state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.state_pause import set_pause
from trading_bot.state_hwm import current_hwm, update_hwm, drawdown_pct


@dataclass
class ReconcileVerdict:
    equity: Decimal
    hwm: float | None
    drawdown_pct: float
    paused: bool


class AccountSentinel:
    def __init__(
        self,
        *,
        engine,
        alpaca,
        pause_flag_path: str | Path,
        max_dd_pct: float,
        account: str,
    ):
        self.engine = engine
        self.alpaca = alpaca
        self.pause_flag_path = Path(pause_flag_path)
        self.max_dd_pct = max_dd_pct
        self.account = account

    def check(self) -> ReconcileVerdict:
        # Independent fetch. Don't trust daemon's equity number.
        acct = self.alpaca.get_account()
        equity = Decimal(str(acct.equity))

        with Session(self.engine) as session:
            update_hwm(session, account=self.account, equity=float(equity))
            hwm = current_hwm(session, account=self.account)
            dd = drawdown_pct(
                session, account=self.account, current_equity=float(equity)
            )

        paused = False
        if dd > self.max_dd_pct:
            set_pause(
                self.pause_flag_path,
                reason=f"drawdown {dd:.2f}% from HWM ${hwm:,.2f}; equity ${equity:,.2f}",
            )
            paused = True

        return ReconcileVerdict(
            equity=equity,
            hwm=hwm,
            drawdown_pct=dd,
            paused=paused,
        )
