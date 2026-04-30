import datetime as dt
import pytest
from trading_bot.options.chain import (
    ChainContract, pick_csp_contract, pick_cc_contract, passes_liquidity,
)
from trading_bot.config import WheelConfig


def _c(strike, kind, delta, *, dte=35, bid=2.5, ask=2.60, oi=500, iv=0.30):
    """Bucket C: bid bumped 2.0 -> 2.5 so the default 35-DTE / strike-190
    contract clears the WheelConfig.min_annualized_yield (12%) gate.
    Tests that need to fail liquidity / premium gates pass explicit bids.
    """
    today = dt.date(2026, 4, 28)
    exp = today + dt.timedelta(days=dte)
    return ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}{kind}{int(strike*1000):08d}",
        underlying="AAPL", expiration=exp, kind=kind, strike=strike,
        bid=bid, ask=ask, last=bid + 0.05, volume=100, open_interest=oi,
        implied_volatility=iv, delta=delta,
    )


def test_pick_csp_chooses_closest_to_target_within_band():
    cfg = WheelConfig(enabled=True)
    chain = [
        _c(200, "P", -0.18),
        _c(195, "P", -0.22),
        _c(190, "P", -0.27),  # closest to 0.25 inside [0.20, 0.30]
        _c(185, "P", -0.33),
    ]
    today = dt.date(2026, 4, 28)
    pick = pick_csp_contract(chain, cfg=cfg, today=today)
    assert pick is not None and pick.strike == 190


def test_pick_csp_returns_none_when_no_contract_in_delta_band():
    cfg = WheelConfig(enabled=True)
    chain = [_c(200, "P", -0.10), _c(180, "P", -0.40)]  # all outside band
    today = dt.date(2026, 4, 28)
    assert pick_csp_contract(chain, cfg=cfg, today=today) is None


def test_pick_csp_skips_contracts_outside_dte_window():
    cfg = WheelConfig(enabled=True)
    chain = [_c(190, "P", -0.25, dte=10), _c(190, "P", -0.25, dte=70)]
    today = dt.date(2026, 4, 28)
    assert pick_csp_contract(chain, cfg=cfg, today=today) is None


def test_pick_cc_requires_strike_at_or_above_cost_basis():
    cfg = WheelConfig(enabled=True)
    chain = [
        _c(195, "C", 0.27),  # below cost basis 200 — disallowed
        _c(205, "C", 0.25),
        _c(215, "C", 0.18),  # outside delta band
    ]
    today = dt.date(2026, 4, 28)
    pick = pick_cc_contract(chain, cost_basis=200.0, cfg=cfg, today=today)
    assert pick is not None and pick.strike == 205


def test_passes_liquidity_spread_pct_path():
    cfg = WheelConfig(enabled=True)
    c = _c(190, "P", -0.25, bid=2.0, ask=2.08, oi=200)  # 4% spread
    assert passes_liquidity(c, cfg) is True


def test_passes_liquidity_absolute_path():
    cfg = WheelConfig(enabled=True)
    c = _c(190, "P", -0.25, bid=0.50, ask=0.58, oi=200)  # 16% but $0.08 absolute
    assert passes_liquidity(c, cfg) is True


def test_passes_liquidity_fails_low_oi():
    cfg = WheelConfig(enabled=True)
    c = _c(190, "P", -0.25, oi=50)
    assert passes_liquidity(c, cfg) is False
