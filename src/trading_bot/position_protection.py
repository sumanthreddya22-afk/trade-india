"""Open-position auto-protect — decides whether to place a protective stop or
market-flatten an unprotected open position, then carries out the action.

Triggered from cli.py:verify_stops every :20 / :50 of every hour.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


def _decide(
    *, current_price: float, ema_20: float, stop_pct: Decimal,
) -> tuple[Literal["protect", "flatten"], float]:
    """Compute strategy-aligned protective stop and decide the action.

    Mirrors MomentumStrategy.evaluate's stop math:
        stop = max(ema_20, last_close * (1 - stop_pct))

    Returns ('protect', stop_level) when stop < current_price (position is
    above its protective floor), or ('flatten', stop_level) when the floor
    has already been crossed.
    """
    pct_stop = current_price * (1.0 - float(stop_pct))
    stop = max(ema_20, pct_stop)
    decision: Literal["protect", "flatten"] = (
        "protect" if stop < current_price else "flatten"
    )
    return decision, stop


from trading_bot.alpaca_client import (
    AlpacaClient, AssetClass, OrderSide, Position,
)
from trading_bot.exceptions import AlpacaClientError
from trading_bot.market_data import MarketDataClient, compute_indicators


@dataclass(frozen=True)
class ProtectionAction:
    """Result of attempting to protect or close one unprotected position."""
    symbol: str
    qty: Decimal
    position_side: OrderSide
    asset_class: AssetClass
    outcome: Literal[
        "stop_placed", "flattened", "failed", "deferred_off_hours"
    ]
    # Populated for stop_placed.
    stop_price: float | None = None
    current_price: float | None = None
    # Populated for flattened (estimate based on last close — actual fill price unknown).
    fill_estimate: float | None = None
    # Populated for failed.
    error: str | None = None


def _classify_asset(raw: str) -> AssetClass:
    """Position.asset_class is a free-form string from Alpaca; normalise."""
    s = raw.lower()
    if "crypto" in s:
        return AssetClass.CRYPTO
    if "option" in s:
        return AssetClass.OPTION
    return AssetClass.STOCK


def _position_side(qty: Decimal) -> OrderSide:
    return OrderSide.BUY if qty >= 0 else OrderSide.SELL


def evaluate_and_act(
    *,
    client: AlpacaClient,
    market_data: MarketDataClient,
    unprotected: list[Position],
    stop_pct: Decimal,
    now_in_market_hours: bool,
) -> list[ProtectionAction]:
    """For each unprotected position: compute the strategy-aligned stop, then
    place it (healthy) or market-flatten (broken). Off-hours stocks defer the
    flatten path because Alpaca rejects equity market orders outside RTH.

    Failures (market-data or order-submit) are captured per-symbol so one bad
    apple doesn't abort the sweep.
    """
    actions: list[ProtectionAction] = []
    for pos in unprotected:
        asset_class = _classify_asset(pos.asset_class)
        side = _position_side(pos.qty)
        abs_qty = abs(pos.qty)

        try:
            bars = market_data.get_daily_bars(pos.symbol, lookback_days=60)
            ind = compute_indicators(bars)
        except (AlpacaClientError, ValueError) as e:
            actions.append(ProtectionAction(
                symbol=pos.symbol, qty=abs_qty, position_side=side,
                asset_class=asset_class, outcome="failed", error=str(e),
            ))
            continue

        decision, stop_level = _decide(
            current_price=float(pos.current_price),
            ema_20=ind.ema_20,
            stop_pct=stop_pct,
        )

        # Off-hours stock that needs flattening: defer.
        if (
            decision == "flatten"
            and asset_class == AssetClass.STOCK
            and not now_in_market_hours
        ):
            actions.append(ProtectionAction(
                symbol=pos.symbol, qty=abs_qty, position_side=side,
                asset_class=asset_class, outcome="deferred_off_hours",
            ))
            continue

        try:
            if decision == "protect":
                client.place_protective_stop(
                    symbol=pos.symbol, qty=abs_qty,
                    position_side=side, asset_class=asset_class,
                    stop_price=Decimal(str(stop_level)).quantize(Decimal("0.01")),
                )
                actions.append(ProtectionAction(
                    symbol=pos.symbol, qty=abs_qty, position_side=side,
                    asset_class=asset_class, outcome="stop_placed",
                    stop_price=stop_level, current_price=float(pos.current_price),
                ))
            else:
                close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
                client.place_market_order(
                    symbol=pos.symbol, qty=float(abs_qty),
                    side=close_side, asset_class=asset_class,
                )
                actions.append(ProtectionAction(
                    symbol=pos.symbol, qty=abs_qty, position_side=side,
                    asset_class=asset_class, outcome="flattened",
                    fill_estimate=float(pos.current_price),
                ))
        except AlpacaClientError as e:
            actions.append(ProtectionAction(
                symbol=pos.symbol, qty=abs_qty, position_side=side,
                asset_class=asset_class, outcome="failed", error=str(e),
            ))
    return actions
