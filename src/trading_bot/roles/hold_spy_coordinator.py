"""Hold-SPY Coordinator — Tier 4 daemon role (Role 15).

Manages the 5-day transition into and out of fallback (passive SPY hold):

  Exit phase (fallback flag flipped ON):
    Day 1: snapshot active-strategy positions; mark transition state.
    Day 2-5: each at 15:55 ET, sell 1/5 of remaining active positions
             (market orders), use the freed cash to buy SPY market.

  Reverse phase (fallback flipped OFF):
    Day 1-5: sell 1/5 of SPY position each day; existing scanner roles
             will repopulate active positions naturally as the regime
             cycles.

Idempotent: invoking twice on the same calendar day is a no-op.
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.shared.alpaca_client import AlpacaClient, OrderSide, AssetClass
from trading_bot.shared.config import Settings
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import (
    FallbackFlag,
    HoldSpyTransitionState,
    RoleRun,
)
from trading_bot.state_fallback import current_flag


TRANSITION_DAYS = 5  # spec §11
SPY_SYMBOL = "SPY"


class HoldSpyCoordinatorRole(BaseRole):
    name = "hold_spy_coordinator"
    tier = 4
    process = "daemon"
    job_description = (
        "On fallback flag transitions, drives 5-day exit/reverse: 1/5 of "
        "active positions liquidated daily at 15:55 ET, freed equity into SPY. "
        "Symmetric reverse on resume."
    )
    sla_seconds = 120
    upstream_roles = ["strategy_coach"]
    downstream_roles = ["trade_executor"]

    def __init__(self, *, engine, alpaca_client: AlpacaClient | None = None):
        super().__init__(engine=engine)
        self._client = alpaca_client

    def _alpaca(self) -> AlpacaClient:
        if self._client is None:
            self._client = AlpacaClient(Settings())
        return self._client

    def _do_work(self, ctx):
        with Session(self.engine) as session:
            current = current_flag(session)
        if current is None:
            return {"skipped": True, "reason": "no_fallback_flag"}

        target_phase = "exit" if current.fallback_active else "reverse"
        # Locate or create transition state for this flag.
        with Session(self.engine) as session:
            transition = (
                session.query(HoldSpyTransitionState)
                .filter(HoldSpyTransitionState.fallback_flag_id == current.id)
                .first()
            )
            if transition is None:
                transition = HoldSpyTransitionState(
                    fallback_flag_id=current.id,
                    phase=target_phase,
                    day_index=0,
                    last_action_at=None,
                )
                session.add(transition)
                session.commit()
                session.refresh(transition)
            tid = transition.id
            t_phase = transition.phase
            t_day = transition.day_index
            t_last = transition.last_action_at

        # Idempotency: already acted today? Compare as tz-aware datetimes so
        # SQLite's tz-stripping on read doesn't cause spurious false-negatives.
        now_utc = dt.datetime.now(dt.timezone.utc)
        start_of_today = dt.datetime.combine(
            now_utc.date(), dt.time.min, tzinfo=dt.timezone.utc
        )
        if t_last is not None:
            t_last_aware = (
                t_last if t_last.tzinfo else t_last.replace(tzinfo=dt.timezone.utc)
            )
            if t_last_aware >= start_of_today:
                return {
                    "skipped": True,
                    "reason": "already_acted_today",
                    "phase": t_phase,
                    "day_index": t_day,
                }

        if t_day >= TRANSITION_DAYS:
            return {
                "skipped": True,
                "reason": "transition_complete",
                "phase": t_phase,
            }

        # Execute one day's slice.
        if t_phase == "exit":
            actions = self._exit_slice(t_day)
        else:
            actions = self._reverse_slice(t_day)

        # Advance state.
        now = dt.datetime.now(dt.timezone.utc)
        with Session(self.engine) as session:
            row = session.get(HoldSpyTransitionState, tid)
            row.day_index = t_day + 1
            row.last_action_at = now
            session.commit()

        return {
            "phase": t_phase,
            "day_index_advanced_to": t_day + 1,
            "actions": actions,
        }

    def _exit_slice(self, day_index: int) -> list[dict]:
        """Sell 1/(remaining_days) of each active-strategy position; buy SPY with freed cash."""
        client = self._alpaca()
        positions = client.get_positions()
        actions: list[dict] = []
        remaining_days = TRANSITION_DAYS - day_index
        if remaining_days <= 0:
            return actions

        freed_cash = Decimal("0")
        for p in positions:
            if p.symbol == SPY_SYMBOL:
                continue
            qty = Decimal(str(p.qty)).copy_abs()
            slice_qty = (qty / Decimal(remaining_days)).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
            if slice_qty < 1:
                continue
            asset_class = (
                AssetClass.CRYPTO
                if "/" in p.symbol or str(p.asset_class).lower() == "crypto"
                else AssetClass.STOCK
            )
            try:
                order_id = client.place_market_order(
                    symbol=p.symbol,
                    qty=float(slice_qty),
                    side=OrderSide.SELL,
                    asset_class=asset_class,
                )
                actions.append(
                    {"action": "sell", "symbol": p.symbol, "qty": float(slice_qty), "order_id": order_id}
                )
                last_price = Decimal(str(p.market_value)) / qty if qty > 0 else Decimal("0")
                freed_cash += slice_qty * last_price
            except Exception as e:
                actions.append({"action": "sell_failed", "symbol": p.symbol, "error": str(e)})

        # Use freed cash to buy SPY (best-effort price estimate; Risk Officer will gate).
        if freed_cash > 0:
            try:
                # Estimate SPY shares from the current SPY market value (or a sentinel
                # price if no SPY position yet). Risk Officer + Trade Executor own
                # final pricing during fill.
                spy_price = self._spy_estimate_price(positions)
                if spy_price > 0:
                    spy_qty = (freed_cash / spy_price).quantize(
                        Decimal("1"), rounding=ROUND_DOWN
                    )
                    if spy_qty >= 1:
                        order_id = client.place_market_order(
                            symbol=SPY_SYMBOL,
                            qty=float(spy_qty),
                            side=OrderSide.BUY,
                            asset_class=AssetClass.STOCK,
                        )
                        actions.append(
                            {"action": "buy_spy", "qty": float(spy_qty), "order_id": order_id}
                        )
            except Exception as e:
                actions.append({"action": "buy_spy_failed", "error": str(e)})

        return actions

    def _reverse_slice(self, day_index: int) -> list[dict]:
        """Sell 1/(remaining_days) of the SPY position; freed cash flows to scanners naturally."""
        client = self._alpaca()
        positions = client.get_positions()
        actions: list[dict] = []
        remaining_days = TRANSITION_DAYS - day_index
        if remaining_days <= 0:
            return actions

        for p in positions:
            if p.symbol != SPY_SYMBOL:
                continue
            qty = Decimal(str(p.qty)).copy_abs()
            slice_qty = (qty / Decimal(remaining_days)).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
            if slice_qty < 1:
                continue
            try:
                order_id = client.place_market_order(
                    symbol=SPY_SYMBOL,
                    qty=float(slice_qty),
                    side=OrderSide.SELL,
                    asset_class=AssetClass.STOCK,
                )
                actions.append(
                    {"action": "sell_spy", "qty": float(slice_qty), "order_id": order_id}
                )
            except Exception as e:
                actions.append({"action": "sell_spy_failed", "error": str(e)})

        return actions

    def _spy_estimate_price(self, positions: list) -> Decimal:
        """Best-effort SPY price from an existing SPY position, else 500 sentinel."""
        for p in positions:
            if p.symbol == SPY_SYMBOL:
                qty = Decimal(str(p.qty))
                if qty != 0:
                    return Decimal(str(p.market_value)) / qty.copy_abs()
        return Decimal("500")  # sentinel used only when no SPY position yet

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "transition_runs",
            float(count),
            f"{count} hold-SPY transition steps in last {lookback_days}d",
        )
