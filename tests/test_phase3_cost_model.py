"""Phase 3 — three-lens cost model (NSE/BSE stocks, crypto, NSE F&O).

Each lens applies the Plan §9 formula. The pessimistic lens IS the
validation gate; broker_paper is the optimistic baseline; raw is
diagnostic only.

India fee math is cross-checked against Zerodha's published charges
list (https://zerodha.com/charges). Numbers here use the locked
cost_model values; if the lock changes, these tests will catch it.
"""
from __future__ import annotations

import pytest

from trading_bot.execution.cost_model import (
    FillCost, OPTIONS_MULTIPLIER, apply_lens, crypto_fill, options_fill,
    stocks_fill,
)
from trading_bot.risk import load_policy

# Use the real cost_model.lock numbers so the test catches lock drift.
_BUNDLE = load_policy()
_COST = _BUNDLE.cost_model
_STOCKS = _COST["stocks"]
_CRYPTO = _COST["crypto"]
_OPTIONS = _COST["options"]


# ---------------------------------------------------------------------------
# Stocks (NSE/BSE — CNC delivery)
# ---------------------------------------------------------------------------


def test_stocks_raw_lens_returns_mid() -> None:
    fc = stocks_fill(lens="raw", side="buy", mid=100, bid=99.95, ask=100.05,
                     qty=10, lock=_STOCKS)
    assert fc.fill_price == 100


def test_stocks_pessimistic_buy_adds_costs() -> None:
    # mid=100, half_spread=0.05, extra_slip=100*5bps=0.05
    # buy_fill = 100 + 0.05 + 0.05 = 100.10
    fc = stocks_fill(lens="pessimistic", side="buy", mid=100,
                     bid=99.95, ask=100.05, qty=10, lock=_STOCKS)
    assert abs(fc.fill_price - 100.10) < 1e-9


def test_stocks_pessimistic_sell_subtracts_costs_and_charges_indian_fees() -> None:
    """NSE delivery sell: STT (0.1%) + exchange + SEBI + GST. No stamp
    duty on sell. No brokerage for CNC."""
    fc = stocks_fill(lens="pessimistic", side="sell", mid=100,
                     bid=99.95, ask=100.05, qty=100, lock=_STOCKS)
    # Price: 100 - 0.05 - 0.05 = 99.90
    assert abs(fc.fill_price - 99.90) < 1e-9

    # Fees on notional=10,000:
    #   stt          = 0.001    * 10,000 = 10.00
    #   exchange     = 3.25e-5  * 10,000 =  0.325
    #   sebi         = 1e-6     * 10,000 =  0.01
    #   stamp        = 0 (sell side)
    #   brokerage    = 0 (CNC)
    #   gst          = 0.18 * (0 + 0.325 + 0.01) = 0.0603
    #   total = 10.3953
    assert fc.fees_breakdown["stt"] == pytest.approx(10.00, abs=1e-6)
    assert fc.fees_breakdown["exchange_txn"] == pytest.approx(0.325, abs=1e-6)
    assert fc.fees_breakdown["sebi_turnover"] == pytest.approx(0.01, abs=1e-6)
    assert fc.fees_breakdown["stamp_duty"] == 0.0
    assert fc.fees_breakdown["brokerage"] == 0.0
    assert fc.fees_breakdown["gst"] == pytest.approx(0.0603, abs=1e-6)
    assert fc.fees_total == pytest.approx(10.3953, abs=1e-6)


def test_stocks_pessimistic_buy_charges_stamp_duty() -> None:
    """Stamp duty applies on BUY only (0.015% per Finance Act 2019)."""
    fc = stocks_fill(lens="pessimistic", side="buy", mid=100,
                     bid=99.95, ask=100.05, qty=100, lock=_STOCKS)
    # notional 10,000 * 0.00015 = 1.50
    assert fc.fees_breakdown["stamp_duty"] == pytest.approx(1.50, abs=1e-6)
    # STT also charged on buy side for delivery: 10.00
    assert fc.fees_breakdown["stt"] == pytest.approx(10.00, abs=1e-6)


def test_stocks_broker_paper_half_of_pessimistic_slip() -> None:
    fc_p = stocks_fill(lens="pessimistic", side="buy", mid=100,
                       bid=99.95, ask=100.05, qty=10, lock=_STOCKS)
    fc_b = stocks_fill(lens="broker_paper", side="buy", mid=100,
                       bid=99.95, ask=100.05, qty=10, lock=_STOCKS)
    # broker_paper omits half-spread and only adds half-bps slip.
    assert fc_b.fill_price < fc_p.fill_price
    assert fc_b.fill_price > 100


def test_stocks_fees_scale_linearly_with_notional() -> None:
    """No per-share caps in the Indian fee stack (unlike US FINRA TAF).
    A 10× notional should produce 10× percentage-based fees."""
    fc_small = stocks_fill(lens="pessimistic", side="sell", mid=10,
                           bid=9.99, ask=10.01, qty=1_000, lock=_STOCKS)
    fc_big = stocks_fill(lens="pessimistic", side="sell", mid=10,
                         bid=9.99, ask=10.01, qty=10_000, lock=_STOCKS)
    # STT (0.1%) must scale exactly 10×.
    assert fc_big.fees_breakdown["stt"] == pytest.approx(
        10.0 * fc_small.fees_breakdown["stt"], rel=1e-9,
    )


# ---------------------------------------------------------------------------
# Crypto (CoinDCX-style INR pairs)
# ---------------------------------------------------------------------------


def test_crypto_pessimistic_buy() -> None:
    """taker_bps=20, extra_slippage_bps=15.
    mid=80000, qty=0.01. notional=800 (below TDS threshold).
      taker_per_unit = 80000 * 0.002 = 160
      extra_per_unit = 80000 * 0.0015 = 120
      buy_fill = 80000 + 160 + 120 = 80280"""
    fc = crypto_fill(lens="pessimistic", side="buy", mid=80000,
                     qty=0.01, lock=_CRYPTO)
    assert fc.fill_price == pytest.approx(80280, abs=1e-6)


def test_crypto_pessimistic_sell_no_tds_below_threshold() -> None:
    """Sell below ₹10,000 notional: taker fee only, no TDS."""
    fc = crypto_fill(lens="pessimistic", side="sell_to_close", mid=80000,
                     qty=0.01, lock=_CRYPTO)
    # notional 800 < 10,000 → no TDS.
    assert fc.fill_price == pytest.approx(79720, abs=1e-6)
    assert fc.fees_breakdown["tds"] == 0.0
    assert fc.fees_breakdown["taker"] == pytest.approx(160 * 0.01, abs=1e-6)


def test_crypto_pessimistic_sell_charges_tds_above_threshold() -> None:
    """Section 194S: 1% TDS on crypto sells where trade value ≥ ₹10,000."""
    # qty=1 → notional 80,000 → well above ₹10,000 threshold.
    fc = crypto_fill(lens="pessimistic", side="sell_to_close", mid=80000,
                     qty=1, lock=_CRYPTO)
    # TDS = 1% of 80,000 = 800.
    assert fc.fees_breakdown["tds"] == pytest.approx(800.0, abs=1e-6)


def test_crypto_buy_never_charges_tds() -> None:
    """TDS is on sells only (deducted by the buyer-side exchange)."""
    fc = crypto_fill(lens="pessimistic", side="buy", mid=80000,
                     qty=1, lock=_CRYPTO)
    assert fc.fees_breakdown.get("tds", 0.0) == 0.0


def test_crypto_raw_equals_mid() -> None:
    fc = crypto_fill(lens="raw", side="buy", mid=80000, qty=1, lock=_CRYPTO)
    assert fc.fill_price == 80000


# ---------------------------------------------------------------------------
# Options (NSE F&O — Nifty / BankNifty)
# ---------------------------------------------------------------------------


def test_options_pessimistic_buy() -> None:
    """extra_slippage_bps=20, brokerage=₹20 flat, stamp 0.003% buy side.
    mid=1.00, bid=0.95, ask=1.05, 1 contract, multiplier=100.
      half_spread=0.05, extra_slip=1*0.002=0.002
      buy_fill = 1.052
      premium_notional = 1 * 1 * 100 = 100
      brokerage=20, stt=0, exchange=0.0053, sebi=0.0001, stamp=0.003
      gst = 0.18 * (20 + 0.0053 + 0.0001) = 3.600972
      total = 23.609372"""
    fc = options_fill(lens="pessimistic", side="buy", mid=1.00,
                      bid=0.95, ask=1.05, contracts=1, lock=_OPTIONS)
    assert fc.fill_price == pytest.approx(1.052, abs=1e-9)
    assert fc.fees_breakdown["brokerage"] == pytest.approx(20.0, abs=1e-9)
    assert fc.fees_breakdown["stt"] == 0.0
    assert fc.fees_breakdown["stamp_duty"] == pytest.approx(0.003, abs=1e-9)
    assert fc.fees_total == pytest.approx(23.609372, abs=1e-4)


def test_options_pessimistic_sell_to_close() -> None:
    """Sell side: STT 0.0125% on premium, no stamp duty.
    10 contracts, premium_notional = 1 * 10 * 100 = 1000.
      brokerage=20, stt=0.125, exchange=0.053, sebi=0.001, stamp=0
      gst = 0.18 * (20 + 0.053 + 0.001) = 3.609732
      total = 23.788732"""
    fc = options_fill(lens="pessimistic", side="sell_to_close", mid=1.00,
                      bid=0.95, ask=1.05, contracts=10, lock=_OPTIONS)
    assert fc.fill_price == pytest.approx(0.948, abs=1e-9)
    assert fc.fees_breakdown["brokerage"] == pytest.approx(20.0, abs=1e-9)
    assert fc.fees_breakdown["stt"] == pytest.approx(0.125, abs=1e-9)
    assert fc.fees_breakdown["stamp_duty"] == 0.0
    assert fc.fees_total == pytest.approx(23.788732, abs=1e-4)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_apply_lens_dispatches_by_asset_class() -> None:
    fc_eq = apply_lens(
        lens="pessimistic", asset_class="nse_equity", side="buy",
        mid=100, bid=99.95, ask=100.05, qty=1, cost_model_lock=_COST,
    )
    assert isinstance(fc_eq, FillCost)
    fc_cr = apply_lens(
        lens="pessimistic", asset_class="crypto", side="buy",
        mid=80000, bid=79980, ask=80020, qty=0.001, cost_model_lock=_COST,
    )
    assert fc_cr.fill_price > 80000
    with pytest.raises(ValueError):
        apply_lens(lens="raw", asset_class="weird", side="buy",
                   mid=1, bid=1, ask=1, qty=1, cost_model_lock=_COST)


def test_apply_lens_accepts_legacy_us_equity_alias() -> None:
    """Backward-compat: old code using asset_class='us_equity' still
    routes to the (now India-fee) stocks lens — fees follow the lock,
    not the label."""
    fc = apply_lens(
        lens="pessimistic", asset_class="us_equity", side="buy",
        mid=100, bid=99.95, ask=100.05, qty=10, cost_model_lock=_COST,
    )
    assert isinstance(fc, FillCost)
    assert fc.fees_breakdown is not None
    assert "stt" in fc.fees_breakdown


def test_three_lenses_ordered_pessimistic_worst_for_buyer() -> None:
    raw = stocks_fill(lens="raw", side="buy", mid=100, bid=99.95, ask=100.05,
                      qty=10, lock=_STOCKS).fill_price
    bp = stocks_fill(lens="broker_paper", side="buy", mid=100,
                     bid=99.95, ask=100.05, qty=10, lock=_STOCKS).fill_price
    p = stocks_fill(lens="pessimistic", side="buy", mid=100,
                    bid=99.95, ask=100.05, qty=10, lock=_STOCKS).fill_price
    assert raw < bp < p, "lenses must order: raw < broker_paper < pessimistic for buys"
