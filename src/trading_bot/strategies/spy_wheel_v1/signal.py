"""Wheel signal — what option to sell, given the current state.

This isn't a return-series signal (unlike Dual Momentum). It returns
an *option order intent* directly: which contract, which strike, what
quantity. The runner translates it into an OrderIntent (multi-leg or
single-leg) for the order router.

Decisions:
  * **Underlying:** SPY only (the v1 strategy).
  * **DTE target:** 30 days; pick the chain closest to 30 DTE.
  * **Delta target:** 0.30 absolute. For short puts, pick the strike
    whose delta is closest to -0.30. For short calls, +0.30.
  * **Order qty (contracts):** ``floor(options_buying_power / strike / 100)``
    capped at ``max_contracts_per_week`` (1 by default — single contract
    keeps risk linear and easy to reason about during paper validation).

Why 30 DTE + 0.30 delta?
  * 30 DTE gives the operator a weekly cadence with room for one
    roll if the market gaps.
  * 0.30 delta puts assign ~30% of the time historically — high enough
    that the strategy actually wheels (we need shares to write covered
    calls), low enough that we keep most of the premium.

Why single contract?
  * Bot's start equity ($15k). 1 SPY put at $400 strike = $40k assignment
    notional. We're already > 200% leveraged at 1 contract. Operator
    decides when to scale up.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional


STRATEGY_ID = "SPY_WHEEL_v1"
UNDERLYING = "SPY"

DEFAULT_PARAMS: dict = {
    "dte_target_days": 30,
    "dte_min_days": 21,
    "dte_max_days": 45,
    "target_delta": 0.30,
    "max_contracts_per_week": 1,
    # Black-Scholes risk-free rate for delta calc (recent 3-mo T-bill).
    "risk_free_rate": 0.045,
}


@dataclass(frozen=True)
class WheelSignal:
    decision_date: dt.date
    state: str                       # WheelState.value
    underlying: str
    underlying_price: float
    side: str                        # "put" | "call" | "none"
    action: str                      # "sell_to_open" | "wait" | "hold"
    contract_symbol: Optional[str]   # OCC ticker (e.g. SPY260619P00420000)
    strike: Optional[float]
    expiry: Optional[dt.date]
    delta_estimate: Optional[float]
    mid_price: Optional[float]
    contracts: int
    rationale: str


def pick_expiry(
    available: list[dt.date], *, today: dt.date,
    target_days: int, min_days: int, max_days: int,
) -> Optional[dt.date]:
    """Choose the listed expiry closest to ``target_days`` DTE, within
    the [min_days, max_days] window. None if no eligible expiry."""
    if not available:
        return None
    eligible = [
        e for e in available
        if min_days <= (e - today).days <= max_days
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda e: abs((e - today).days - target_days))


def occ_ticker(
    underlying: str, expiry: dt.date, side: str, strike: float,
) -> str:
    """Build the OCC option symbol used by Alpaca (e.g.
    'SPY250516P00450000' for a $450 SPY put expiring 2025-05-16).
    Strike is encoded as 8 digits × 1000 (so $450 → 00450000)."""
    yymmdd = expiry.strftime("%y%m%d")
    side_char = "C" if side.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying}{yymmdd}{side_char}{strike_int:08d}"


__all__ = [
    "DEFAULT_PARAMS", "STRATEGY_ID", "UNDERLYING",
    "WheelSignal", "occ_ticker", "pick_expiry",
]


# signal_fn alias for parity with other strategies
def signal_fn(
    history, decision_date, *, params=DEFAULT_PARAMS, universe=(UNDERLYING,),
):
    """Stub for the standard strategy signal protocol.

    The wheel doesn't return target weights — it returns an option order.
    Callers should use ``runner.evaluate_strategy`` directly. This
    function exists so the Tier-1 harness can introspect the strategy
    module without crashing; it returns an empty dict.
    """
    return {}
