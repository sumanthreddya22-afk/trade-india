"""Three-lens cost model.

Plan v4 §9: every backtest reports raw / broker_paper / pessimistic. The
validation gate uses ONLY the pessimistic lens. Parameters live in
``policy/cost_model.lock`` and are loaded via the PolicyBundle.

A "lens" is a pure function that, given a fill scenario, returns the
effective fill price + the breakdown of fees and slippage that produced
it. The breakdown is used by ``drift_monitor`` to compare modelled to
realised cost per fill.

Two key choices, kept verbatim from Plan §9:

- **Mid-relative.** ``mid = (bid + ask) / 2``. The model adds half-spread
  + extra-slip on top of the mid, NOT on top of the quoted bid/ask. This
  is what makes the lens "pessimistic" — a marketable limit at the
  formula's price assumes you ALWAYS cross at the worst plausible
  microstructure.
- **Fees on sells (equities only).** SEC Section 31 + FINRA TAF are
  charged on sells only; capped per Plan §9.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

LensT = Literal["raw", "broker_paper", "pessimistic"]
SideT = Literal["buy", "sell", "sell_short", "sell_to_close", "buy_to_close"]


@dataclass(frozen=True)
class FillCost:
    """Result of applying one lens to one fill scenario."""

    lens: LensT
    fill_price: float
    half_spread_cost: float = 0.0
    extra_slip_cost: float = 0.0
    fees_total: float = 0.0
    fees_breakdown: Mapping[str, float] = None  # type: ignore[assignment]

    def total_cost_vs_mid(self) -> float:
        """All-in cost (positive = paid more than mid). For drift monitor."""
        return self.half_spread_cost + self.extra_slip_cost + self.fees_total


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------

_FINRA_TAF_PER_DOLLAR_NOT_USED = None
"""FINRA TAF is per *share*, not per dollar. Plan §9 ships
``finra_taf_per_share`` in the lock; we read it directly."""


def stocks_fill(
    *,
    lens: LensT,
    side: SideT,
    mid: float,
    bid: float,
    ask: float,
    qty: float,
    lock: Mapping,
) -> FillCost:
    """Apply the stocks formula. ``lock`` is ``policy.cost_model['stocks']``."""
    half_spread = (ask - bid) / 2.0

    if lens == "raw":
        return FillCost(lens=lens, fill_price=mid)

    extra_bps = float(lock.get("extra_slippage_bps", 0))
    broker_fee_share = float(lock.get("broker_fees_per_share", 0.0))
    sec31_rate = float(lock.get("sec_section_31_rate", 0.0))
    finra_taf_share = float(lock.get("finra_taf_per_share", 0.0))
    finra_taf_cap = float(lock.get("finra_taf_cap_per_trade", 0.0))

    if lens == "broker_paper":
        # Alpaca's paper modelling: fills at mid + a small fixed slip,
        # no SEC/FINRA fees on the paper venue (it's a known optimism
        # baseline).
        slip = mid * (extra_bps / 10000.0) / 2.0   # half of pessimistic
        if side in ("buy", "buy_to_close"):
            return FillCost(
                lens=lens, fill_price=mid + slip,
                extra_slip_cost=slip * qty,
            )
        return FillCost(
            lens=lens, fill_price=mid - slip,
            extra_slip_cost=slip * qty,
        )

    # pessimistic
    extra_slip = mid * (extra_bps / 10000.0)
    broker_fee_total = broker_fee_share * qty
    if side in ("buy", "buy_to_close"):
        fill_price = mid + half_spread + extra_slip
        return FillCost(
            lens=lens, fill_price=fill_price,
            half_spread_cost=half_spread * qty,
            extra_slip_cost=extra_slip * qty,
            fees_total=broker_fee_total,
            fees_breakdown={"broker": broker_fee_total},
        )
    # sells (incl sell_to_close and sell_short)
    notional = mid * qty
    sec31 = sec31_rate * notional
    finra_taf = min(finra_taf_share * qty, finra_taf_cap)
    fees = broker_fee_total + sec31 + finra_taf
    fill_price = mid - half_spread - extra_slip
    return FillCost(
        lens=lens, fill_price=fill_price,
        half_spread_cost=half_spread * qty,
        extra_slip_cost=extra_slip * qty,
        fees_total=fees,
        fees_breakdown={
            "broker": broker_fee_total, "sec_section_31": sec31,
            "finra_taf": finra_taf,
        },
    )


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------

def crypto_fill(
    *,
    lens: LensT,
    side: SideT,
    mid: float,
    qty: float,
    lock: Mapping,
) -> FillCost:
    if lens == "raw":
        return FillCost(lens=lens, fill_price=mid)

    taker_bps = float(lock.get("taker_bps", 0))
    extra_bps = float(lock.get("extra_slippage_bps", 0))

    taker_cost_per_unit = mid * (taker_bps / 10000.0)
    extra_slip_per_unit = mid * (extra_bps / 10000.0)

    if lens == "broker_paper":
        # Optimistic paper baseline: half the pessimistic.
        if side in ("buy", "buy_to_close"):
            fill = mid + taker_cost_per_unit / 2.0
        else:
            fill = mid - taker_cost_per_unit / 2.0
        return FillCost(
            lens=lens, fill_price=fill,
            fees_total=taker_cost_per_unit / 2.0 * qty,
        )

    # pessimistic
    if side in ("buy", "buy_to_close"):
        fill = mid + taker_cost_per_unit + extra_slip_per_unit
    else:
        fill = mid - taker_cost_per_unit - extra_slip_per_unit
    return FillCost(
        lens=lens, fill_price=fill,
        extra_slip_cost=extra_slip_per_unit * qty,
        fees_total=taker_cost_per_unit * qty,
        fees_breakdown={"taker": taker_cost_per_unit * qty},
    )


# ---------------------------------------------------------------------------
# Options (per contract, multiplier 100)
# ---------------------------------------------------------------------------

OPTIONS_MULTIPLIER = 100


def options_fill(
    *,
    lens: LensT,
    side: SideT,
    mid: float,
    bid: float,
    ask: float,
    contracts: int,
    lock: Mapping,
) -> FillCost:
    if lens == "raw":
        return FillCost(lens=lens, fill_price=mid)

    half_spread = (ask - bid) / 2.0
    extra_bps = float(lock.get("extra_slippage_bps", 0))
    per_contract_fee = float(lock.get("per_contract_fee_usd", 0))

    extra_slip = mid * (extra_bps / 10000.0)

    if lens == "broker_paper":
        if side in ("buy", "buy_to_close"):
            return FillCost(
                lens=lens, fill_price=mid + extra_slip / 2.0,
                extra_slip_cost=extra_slip / 2.0 * contracts * OPTIONS_MULTIPLIER,
            )
        return FillCost(
            lens=lens, fill_price=mid - extra_slip / 2.0,
            extra_slip_cost=extra_slip / 2.0 * contracts * OPTIONS_MULTIPLIER,
        )

    fees = per_contract_fee * contracts
    if side in ("buy", "buy_to_close"):
        fill_price = mid + half_spread + extra_slip
        return FillCost(
            lens=lens, fill_price=fill_price,
            half_spread_cost=half_spread * contracts * OPTIONS_MULTIPLIER,
            extra_slip_cost=extra_slip * contracts * OPTIONS_MULTIPLIER,
            fees_total=fees,
            fees_breakdown={"per_contract": fees},
        )
    fill_price = mid - half_spread - extra_slip
    return FillCost(
        lens=lens, fill_price=fill_price,
        half_spread_cost=half_spread * contracts * OPTIONS_MULTIPLIER,
        extra_slip_cost=extra_slip * contracts * OPTIONS_MULTIPLIER,
        fees_total=fees,
        fees_breakdown={"per_contract": fees},
    )


def apply_lens(
    *,
    lens: LensT,
    asset_class: str,
    side: SideT,
    mid: float,
    bid: float,
    ask: float,
    qty: float,
    cost_model_lock: Mapping,
) -> FillCost:
    """Single-entry dispatch by asset class. ``cost_model_lock`` is the
    whole ``policy.cost_model`` mapping (not a sub-key)."""
    ac = (asset_class or "").lower()
    if ac in ("equity", "us_equity"):
        return stocks_fill(
            lens=lens, side=side, mid=mid, bid=bid, ask=ask, qty=qty,
            lock=cost_model_lock.get("stocks", {}),
        )
    if ac == "crypto":
        return crypto_fill(
            lens=lens, side=side, mid=mid, qty=qty,
            lock=cost_model_lock.get("crypto", {}),
        )
    if ac in ("option", "us_option"):
        return options_fill(
            lens=lens, side=side, mid=mid, bid=bid, ask=ask,
            contracts=int(qty), lock=cost_model_lock.get("options", {}),
        )
    raise ValueError(f"unknown asset_class={asset_class!r}")


__all__ = [
    "FillCost", "LensT", "SideT",
    "apply_lens", "crypto_fill", "options_fill", "stocks_fill",
]
