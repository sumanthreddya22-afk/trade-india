"""Phase 3 — three-lens cost model (stocks, crypto, options).

Each lens applies the Plan §9 formula. The pessimistic lens IS the
validation gate; broker_paper is the optimistic baseline; raw is
diagnostic only.
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
# Stocks
# ---------------------------------------------------------------------------


def test_stocks_raw_lens_returns_mid() -> None:
    fc = stocks_fill(lens="raw", side="buy", mid=100, bid=99.95, ask=100.05,
                     qty=10, lock=_STOCKS)
    assert fc.fill_price == 100


def test_stocks_pessimistic_buy_adds_costs() -> None:
    # mid=100, bid=99.95, ask=100.05 -> half_spread=0.05
    # extra_slippage_bps=5 -> extra=0.05
    # broker_fees_per_share=0
    # buy_fill = 100 + 0.05 + 0.05 = 100.10
    fc = stocks_fill(lens="pessimistic", side="buy", mid=100,
                     bid=99.95, ask=100.05, qty=10, lock=_STOCKS)
    assert abs(fc.fill_price - 100.10) < 1e-9


def test_stocks_pessimistic_sell_subtracts_costs_and_charges_fees() -> None:
    fc = stocks_fill(lens="pessimistic", side="sell", mid=100,
                     bid=99.95, ask=100.05, qty=100, lock=_STOCKS)
    # sell_fill = 100 - 0.05 - 0.05 = 99.90
    assert abs(fc.fill_price - 99.90) < 1e-9
    # fees: sec=100*100*0.0000278=0.278; finra_taf=100*0.000166=0.0166 (well under cap)
    assert fc.fees_total > 0
    assert "sec_section_31" in fc.fees_breakdown
    assert "finra_taf" in fc.fees_breakdown


def test_stocks_broker_paper_half_of_pessimistic_slip() -> None:
    fc_p = stocks_fill(lens="pessimistic", side="buy", mid=100,
                       bid=99.95, ask=100.05, qty=10, lock=_STOCKS)
    fc_b = stocks_fill(lens="broker_paper", side="buy", mid=100,
                       bid=99.95, ask=100.05, qty=10, lock=_STOCKS)
    # broker_paper omits half-spread and only adds half-bps slip.
    assert fc_b.fill_price < fc_p.fill_price
    assert fc_b.fill_price > 100


def test_stocks_taf_cap_applies_on_large_sell() -> None:
    fc = stocks_fill(lens="pessimistic", side="sell", mid=10,
                     bid=9.99, ask=10.01, qty=100_000, lock=_STOCKS)
    # 100k * 0.000166 = 16.6 -> capped at 8.30
    assert fc.fees_breakdown["finra_taf"] == _STOCKS["finra_taf_cap_per_trade"]


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


def test_crypto_pessimistic_buy() -> None:
    # taker_bps=30, extra_slippage_bps=10. mid=80000. qty=0.01.
    # taker_per_unit = 80000 * 0.003 = 240
    # extra_per_unit = 80000 * 0.001 = 80
    # buy_fill = 80000 + 240 + 80 = 80320
    fc = crypto_fill(lens="pessimistic", side="buy", mid=80000,
                     qty=0.01, lock=_CRYPTO)
    assert abs(fc.fill_price - 80320) < 1e-6


def test_crypto_pessimistic_sell_subtracts() -> None:
    fc = crypto_fill(lens="pessimistic", side="sell_to_close", mid=80000,
                     qty=0.01, lock=_CRYPTO)
    assert abs(fc.fill_price - 79680) < 1e-6


def test_crypto_raw_equals_mid() -> None:
    fc = crypto_fill(lens="raw", side="buy", mid=80000, qty=1, lock=_CRYPTO)
    assert fc.fill_price == 80000


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def test_options_pessimistic_buy() -> None:
    # extra_slippage_bps=25, per_contract_fee=0.65
    # mid=1.00, bid=0.95, ask=1.05 -> half_spread=0.05
    # extra = 1.00 * 0.0025 = 0.0025
    # buy_fill = 1.00 + 0.05 + 0.0025 = 1.0525
    fc = options_fill(lens="pessimistic", side="buy", mid=1.00,
                      bid=0.95, ask=1.05, contracts=1, lock=_OPTIONS)
    assert abs(fc.fill_price - 1.0525) < 1e-9
    # fees: 1 * 0.65 = 0.65
    assert abs(fc.fees_total - 0.65) < 1e-9


def test_options_pessimistic_sell_to_close() -> None:
    fc = options_fill(lens="pessimistic", side="sell_to_close", mid=1.00,
                      bid=0.95, ask=1.05, contracts=10, lock=_OPTIONS)
    assert abs(fc.fill_price - 0.9475) < 1e-9
    assert abs(fc.fees_total - 6.50) < 1e-9     # 10 * 0.65


def test_apply_lens_dispatches_by_asset_class() -> None:
    fc_eq = apply_lens(
        lens="pessimistic", asset_class="equity", side="buy",
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


def test_three_lenses_ordered_pessimistic_worst_for_buyer() -> None:
    raw = stocks_fill(lens="raw", side="buy", mid=100, bid=99.95, ask=100.05,
                      qty=10, lock=_STOCKS).fill_price
    bp = stocks_fill(lens="broker_paper", side="buy", mid=100,
                     bid=99.95, ask=100.05, qty=10, lock=_STOCKS).fill_price
    p = stocks_fill(lens="pessimistic", side="buy", mid=100,
                    bid=99.95, ask=100.05, qty=10, lock=_STOCKS).fill_price
    assert raw < bp < p, "lenses must order: raw < broker_paper < pessimistic for buys"
