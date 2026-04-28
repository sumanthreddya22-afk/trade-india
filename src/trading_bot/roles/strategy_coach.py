"""Strategy Coach — Tier 2 daemon role (Role 10).

Once-daily evaluation of 30d paper alpha vs SPY. Flips fallback_active
flag with hysteresis to prevent whipsaw:

  Currently OFF (active strategy):
    flip ON when alpha < 1.5x SPY (today only).

  Currently ON (fallback / hold-SPY):
    flip OFF when alpha > 1.65x SPY today AND > 1.5x for 5 consecutive
    trading days.

Spec §7.3 Role 10 + §11 Beat-SPY logic.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.journal_alpha import compute_journal_alpha_vs_spy
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import FallbackFlag, RoleRun
from trading_bot.state_fallback import current_flag, set_flag


ALPHA_THRESHOLD_LOW = 1.5     # below → enter fallback
ALPHA_THRESHOLD_HIGH = 1.65   # above + sustained → resume
RESUME_HYSTERESIS_DAYS = 5    # days that must all be > 1.5 before resume


class StrategyCoachRole(BaseRole):
    name = "strategy_coach"
    tier = 2
    process = "daemon"
    job_description = (
        "Once-daily evaluation of 30d paper alpha vs SPY; flips fallback_active "
        "flag with hysteresis (1.5x enter, 1.65x + 5d sustained to resume)."
    )
    sla_seconds = 30
    upstream_roles: list[str] = []
    downstream_roles = ["stock_scanner", "crypto_scanner", "hold_spy_coordinator"]

    def __init__(
        self,
        *,
        engine,
        closed_trades_db: str | Path = "data/closed_trades.db",
        starting_equity: Decimal = Decimal("15000"),
    ):
        super().__init__(engine=engine)
        self.closed_trades_db = Path(closed_trades_db)
        self.starting_equity = starting_equity

    def _do_work(self, ctx):
        today = ctx.get("as_of") or dt.date.today()
        today_alpha = self._alpha_at(today)
        if today_alpha["insufficient_data"]:
            return {
                "flag_change": False,
                "reason": "insufficient_data",
                "n_trades": today_alpha["n_trades"],
            }

        with Session(self.engine) as session:
            current = current_flag(session)
            currently_active = bool(current and current.fallback_active)

        alpha_today = today_alpha["alpha_multiplier"]

        if not currently_active:
            # Currently running active strategy. Flip ON if today's alpha drops below 1.5x.
            if alpha_today < ALPHA_THRESHOLD_LOW:
                self._flip(
                    True,
                    reason=f"alpha {alpha_today:.2f} < {ALPHA_THRESHOLD_LOW}x SPY",
                )
                return {
                    "flag_change": True,
                    "new_state": "fallback_active",
                    "alpha_multiplier": alpha_today,
                }
            return {
                "flag_change": False,
                "current_state": "active",
                "alpha_multiplier": alpha_today,
            }

        # Currently in fallback. Resume only with 1.65x today + 5d sustained > 1.5x.
        if alpha_today < ALPHA_THRESHOLD_HIGH:
            return {
                "flag_change": False,
                "current_state": "fallback",
                "alpha_multiplier": alpha_today,
                "reason": f"alpha {alpha_today:.2f} < resume threshold {ALPHA_THRESHOLD_HIGH}",
            }

        # Today crosses 1.65x. Check 5-day sustained > 1.5x.
        sustained = True
        for back in range(1, RESUME_HYSTERESIS_DAYS):
            day_alpha = self._alpha_at(today - dt.timedelta(days=back))
            if day_alpha["insufficient_data"] or day_alpha["alpha_multiplier"] < ALPHA_THRESHOLD_LOW:
                sustained = False
                break

        if sustained:
            self._flip(
                False,
                reason=(
                    f"alpha {alpha_today:.2f} > {ALPHA_THRESHOLD_HIGH}x AND "
                    f">{ALPHA_THRESHOLD_LOW}x sustained {RESUME_HYSTERESIS_DAYS}d"
                ),
            )
            return {
                "flag_change": True,
                "new_state": "active",
                "alpha_multiplier": alpha_today,
            }
        return {
            "flag_change": False,
            "current_state": "fallback",
            "alpha_multiplier": alpha_today,
            "reason": "hysteresis not yet sustained",
        }

    def _flip(self, new_state_active: bool, *, reason: str) -> None:
        with Session(self.engine) as session:
            set_flag(
                session,
                fallback_active=new_state_active,
                set_by="strategy_coach",
                reason=reason,
            )

    def _alpha_at(self, as_of: dt.date) -> dict:
        return compute_journal_alpha_vs_spy(
            closed_trades_db=self.closed_trades_db,
            starting_equity=self.starting_equity,
            as_of=as_of,
        )

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
            flips = (
                session.query(FallbackFlag)
                .filter(
                    FallbackFlag.set_by == "strategy_coach",
                    FallbackFlag.set_at >= cutoff,
                )
                .count()
            )
        return (
            "flag_flips",
            float(flips),
            f"{flips} fallback flips in {count} runs over last {lookback_days}d",
        )
