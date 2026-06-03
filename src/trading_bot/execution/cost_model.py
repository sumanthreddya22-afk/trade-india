"""Three-lens cost model — India / Zerodha edition.

Plan v4 §9: every backtest reports raw / broker_paper / pessimistic.
The validation gate uses ONLY the pessimistic lens. Parameters live in
``policy/cost_model.lock`` and are loaded via the PolicyBundle.

A "lens" is a pure function that, given a fill scenario, returns the
effective fill price + the breakdown of fees and slippage that produced
it. The breakdown is used by ``drift_monitor`` to compare modelled to
realised cost per fill.

Two key choices, carried over from Plan §9:

- **Mid-relative.** ``mid = (bid + ask) / 2``. The model adds half-spread
  + extra-slip on top of the mid, NOT on top of the quoted bid/ask. This
  is what makes the lens "pessimistic" — a marketable limit at the
  formula's price assumes you ALWAYS cross at the worst plausible
  microstructure.
- **Fee-side asymmetry.** Indian regulatory + statutory charges differ
  between buy and sell:

  * NSE/BSE equity (CNC delivery): STT on both sides; stamp duty on buy
    only; exchange + SEBI fees both sides; GST 18% on brokerage +
    exchange + SEBI (NOT on STT or stamp duty).
  * NSE F&O options: brokerage ₹20 flat per order; STT on sell side
    only (0.0125% of premium); exchange + SEBI + GST as above; stamp
    duty on buy side.
  * Crypto (CoinDCX/WazirX INR pairs): taker fee both sides; 1% TDS on
    sell when trade value ≥ ₹10,000 (Section 194S Income Tax Act).
    30% flat tax on gains (Section 115BBH) is an annual filing concern,
    NOT a per-fill cost — excluded here.

The pessimistic lens is the gate. broker_paper omits statutory fees
(it's the "what Zerodha's paper account would show you" optimism
baseline). Raw = mid (diagnostic only).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

LensT = Literal["raw", "broker_paper", "pessimistic"]
SideT = Literal["buy", "sell", "sell_short", "sell_to_close", "buy_to_close"]

_BUY_SIDES = ("buy", "buy_to_close")

# TDS threshold for crypto sells per Section 194S (₹10,000 in general).
_CRYPTO_TDS_THRESHOLD_INR = 10_000.0


@dataclass(frozen=True)
class FillCost:
    """Result of applying one lens to one fill scenario.

    ``fees_total`` is the sum of every statutory + brokerage charge on
    the trade (excluding bid-ask half-spread and slippage, which are
    tracked separately on the price). All values in account currency
    (INR for India, USD for the legacy lens — same numeric semantics).
    """

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
# Stocks (NSE/BSE equity — CNC delivery)
# ---------------------------------------------------------------------------

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
    """Apply the NSE/BSE equity formula. ``lock`` is the ``stocks``
    sub-mapping of ``policy.cost_model``.

    Assumes CNC (delivery) product. MIS (intraday) would use
    ``zerodha_intraday_flat_inr`` for brokerage and a different stamp
    duty rate; not modelled until a strategy needs it.
    """
    half_spread = (ask - bid) / 2.0
    is_buy = side in _BUY_SIDES

    if lens == "raw":
        return FillCost(lens=lens, fill_price=mid)

    extra_bps = float(lock.get("extra_slippage_bps", 0))
    extra_slip = mid * (extra_bps / 10000.0)

    if lens == "broker_paper":
        # "Zerodha paper" optimism baseline: half the pessimistic slip,
        # no statutory fees (the paper venue doesn't bill them).
        slip = extra_slip / 2.0
        if is_buy:
            return FillCost(
                lens=lens, fill_price=mid + slip,
                extra_slip_cost=slip * qty,
            )
        return FillCost(
            lens=lens, fill_price=mid - slip,
            extra_slip_cost=slip * qty,
        )

    # pessimistic — full Indian fee stack
    if is_buy:
        fill_price = mid + half_spread + extra_slip
    else:
        fill_price = mid - half_spread - extra_slip

    notional = mid * qty

    # CNC delivery: Zerodha charges ₹0 brokerage on equity delivery.
    brokerage = 0.0

    # STT (Securities Transaction Tax). 0.1% each side for delivery.
    if is_buy:
        stt_rate = float(lock.get("stt_delivery_buy_pct", 0.0))
    else:
        stt_rate = float(lock.get("stt_delivery_sell_pct", 0.0))
    stt = stt_rate * notional

    # Exchange transaction charge (NSE). 0.00325% (₹325/crore) both sides.
    exchange = float(lock.get("exchange_txn_charge_pct", 0.0)) * notional

    # SEBI turnover fee. 0.0001% (₹10/crore) both sides.
    sebi = float(lock.get("sebi_turnover_fee_pct", 0.0)) * notional

    # Stamp duty: 0.015% on buy side only (Finance Act 2019).
    if is_buy:
        stamp = float(lock.get("stamp_duty_buy_pct", 0.0)) * notional
    else:
        stamp = 0.0

    # GST: 18% on (brokerage + exchange + SEBI). NOT on STT or stamp duty.
    gst_rate = float(lock.get("gst_on_brokerage_pct", 0.0)) / 100.0
    gst = gst_rate * (brokerage + exchange + sebi)

    fees_total = brokerage + stt + exchange + sebi + stamp + gst

    return FillCost(
        lens=lens, fill_price=fill_price,
        half_spread_cost=half_spread * qty,
        extra_slip_cost=extra_slip * qty,
        fees_total=fees_total,
        fees_breakdown={
            "brokerage": brokerage,
            "stt": stt,
            "exchange_txn": exchange,
            "sebi_turnover": sebi,
            "stamp_duty": stamp,
            "gst": gst,
        },
    )


# ---------------------------------------------------------------------------
# Crypto (CoinDCX / WazirX — INR pairs)
# ---------------------------------------------------------------------------

def crypto_fill(
    *,
    lens: LensT,
    side: SideT,
    mid: float,
    qty: float,
    lock: Mapping,
) -> FillCost:
    """Apply the Indian crypto formula. ``lock`` is the ``crypto``
    sub-mapping of ``policy.cost_model``.

    Models: taker fee both sides + 1% TDS on sell side when trade
    value ≥ ₹10,000 (Section 194S, in effect since 2022-07-01).
    """
    is_buy = side in _BUY_SIDES

    if lens == "raw":
        return FillCost(lens=lens, fill_price=mid)

    taker_bps = float(lock.get("taker_bps", 0))
    extra_bps = float(lock.get("extra_slippage_bps", 0))

    taker_cost_per_unit = mid * (taker_bps / 10000.0)
    extra_slip_per_unit = mid * (extra_bps / 10000.0)

    if lens == "broker_paper":
        # Optimistic: half the pessimistic taker cost, no TDS modelled.
        half_taker = taker_cost_per_unit / 2.0
        fill = mid + half_taker if is_buy else mid - half_taker
        return FillCost(
            lens=lens, fill_price=fill,
            fees_total=half_taker * qty,
        )

    # pessimistic
    if is_buy:
        fill = mid + taker_cost_per_unit + extra_slip_per_unit
    else:
        fill = mid - taker_cost_per_unit - extra_slip_per_unit

    notional = mid * qty
    taker_fee_total = taker_cost_per_unit * qty

    # TDS only on sells, and only if trade value ≥ threshold.
    tds_total = 0.0
    if not is_buy:
        tds_pct = float(lock.get("tds_pct", 0.0)) / 100.0
        if notional >= _CRYPTO_TDS_THRESHOLD_INR:
            tds_total = tds_pct * notional

    fees_total = taker_fee_total + tds_total

    return FillCost(
        lens=lens, fill_price=fill,
        extra_slip_cost=extra_slip_per_unit * qty,
        fees_total=fees_total,
        fees_breakdown={"taker": taker_fee_total, "tds": tds_total},
    )


# ---------------------------------------------------------------------------
# Options (NSE F&O — Nifty / BankNifty / FinNifty)
# ---------------------------------------------------------------------------

# Standard NSE index-option lot multipliers vary (NIFTY=50, BANKNIFTY=15).
# 100 was the US default; keep for backward-compat callers and override
# at the strategy level where the actual contract lot size is known.
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
    """Apply the NSE F&O options formula. ``lock`` is the ``options``
    sub-mapping of ``policy.cost_model``.

    Models: ₹20 flat brokerage per order + STT on sell side (0.0125% of
    premium) + exchange + SEBI + stamp duty on buy + GST.
    """
    is_buy = side in _BUY_SIDES

    if lens == "raw":
        return FillCost(lens=lens, fill_price=mid)

    half_spread = (ask - bid) / 2.0
    extra_bps = float(lock.get("extra_slippage_bps", 0))
    extra_slip = mid * (extra_bps / 10000.0)

    if lens == "broker_paper":
        slip = extra_slip / 2.0
        if is_buy:
            return FillCost(
                lens=lens, fill_price=mid + slip,
                extra_slip_cost=slip * contracts * OPTIONS_MULTIPLIER,
            )
        return FillCost(
            lens=lens, fill_price=mid - slip,
            extra_slip_cost=slip * contracts * OPTIONS_MULTIPLIER,
        )

    # pessimistic
    if is_buy:
        fill_price = mid + half_spread + extra_slip
    else:
        fill_price = mid - half_spread - extra_slip

    # Premium notional (₹ value of the trade).
    premium_notional = mid * contracts * OPTIONS_MULTIPLIER

    # Zerodha F&O brokerage: ₹20 flat per order.
    brokerage = float(lock.get("zerodha_fo_brokerage_flat_inr", 0.0))

    # STT on sell side only (0.0125% of premium).
    if is_buy:
        stt = 0.0
    else:
        stt = float(lock.get("stt_fo_sell_pct", 0.0)) * premium_notional

    # NSE F&O transaction charge (0.053% — but spec'd as fraction in lock).
    exchange = float(lock.get("nse_fo_txn_charge_pct", 0.0)) * premium_notional

    # SEBI turnover fee.
    sebi = float(lock.get("sebi_fo_turnover_fee_pct", 0.0)) * premium_notional

    # Stamp duty: 0.003% on buy side only.
    if is_buy:
        stamp = float(lock.get("stamp_duty_fo_buy_pct", 0.0)) * premium_notional
    else:
        stamp = 0.0

    # GST: 18% on (brokerage + exchange + SEBI).
    gst_rate = float(lock.get("gst_on_brokerage_pct", 0.0)) / 100.0
    gst = gst_rate * (brokerage + exchange + sebi)

    fees_total = brokerage + stt + exchange + sebi + stamp + gst

    return FillCost(
        lens=lens, fill_price=fill_price,
        half_spread_cost=half_spread * contracts * OPTIONS_MULTIPLIER,
        extra_slip_cost=extra_slip * contracts * OPTIONS_MULTIPLIER,
        fees_total=fees_total,
        fees_breakdown={
            "brokerage": brokerage,
            "stt": stt,
            "exchange_txn": exchange,
            "sebi_turnover": sebi,
            "stamp_duty": stamp,
            "gst": gst,
        },
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
    if ac in ("equity", "us_equity", "nse_equity", "bse_equity"):
        return stocks_fill(
            lens=lens, side=side, mid=mid, bid=bid, ask=ask, qty=qty,
            lock=cost_model_lock.get("stocks", {}),
        )
    if ac == "crypto":
        return crypto_fill(
            lens=lens, side=side, mid=mid, qty=qty,
            lock=cost_model_lock.get("crypto", {}),
        )
    if ac in ("option", "us_option", "nse_option", "nfo_option"):
        return options_fill(
            lens=lens, side=side, mid=mid, bid=bid, ask=ask,
            contracts=int(qty), lock=cost_model_lock.get("options", {}),
        )
    raise ValueError(f"unknown asset_class={asset_class!r}")


__all__ = [
    "FillCost", "LensT", "SideT",
    "apply_lens", "crypto_fill", "options_fill", "stocks_fill",
]
