"""End-to-end Phase 3 tests: hot-path code consults threshold_overrides
when an engine is supplied, falls back to static cfg otherwise.

These tests are the contract between the tuner and the consumers:

  * RiskManager.per_trade_risk_pct & max_position_pct read overrides
    when engine is set; static cfg when engine is None.
  * WheelLane.passes_preflight uses iv_rank_floor override.
  * pick_csp_contract / pick_cc_contract use min_premium_abs +
    min_annualized_yield overrides.

Bound clamping is enforced at the threshold_overrides layer; here we
just verify the end-to-end dispatch.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from trading_bot.shared.config import WheelConfig
from trading_bot.options.chain import ChainContract, pick_csp_contract
from trading_bot.options.wheel_lane import WheelLane, WheelInputs
from trading_bot.state_db import Base, get_engine
from trading_bot.threshold_overrides import write_override


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# RiskManager — per_trade_risk_pct and max_position_pct
# ---------------------------------------------------------------------------


def test_risk_manager_reads_per_trade_risk_override(engine):
    """When the override is tighter than YAML, RiskManager uses the override.

    We don't run a full check() (that needs an OrderRequest); we just hit the
    private resolver directly — that's the boundary the tuner controls.
    """
    from trading_bot.shared.risk_manager import RiskManager

    class _Risk:
        per_trade_risk_pct = 1.0
        max_position_pct = 10.0

    class _Cfg:
        risk = _Risk()

    rm = RiskManager(_Cfg(), engine=engine)
    # No override yet → static
    assert rm._per_trade_risk_pct() == pytest.approx(1.0)
    write_override(
        engine, knob="per_trade_risk_pct",
        value=0.5, bounds_min=0.5, bounds_max=2.0,
    )
    assert rm._per_trade_risk_pct() == pytest.approx(0.5)


def test_risk_manager_falls_back_to_static_without_engine():
    from trading_bot.shared.risk_manager import RiskManager

    class _Risk:
        per_trade_risk_pct = 1.0
        max_position_pct = 10.0

    class _Cfg:
        risk = _Risk()

    rm = RiskManager(_Cfg())  # no engine
    assert rm._per_trade_risk_pct() == pytest.approx(1.0)
    assert rm._max_position_pct() == pytest.approx(10.0)


def test_risk_manager_max_position_override(engine):
    from trading_bot.shared.risk_manager import RiskManager

    class _Risk:
        per_trade_risk_pct = 1.0
        max_position_pct = 10.0

    class _Cfg:
        risk = _Risk()

    rm = RiskManager(_Cfg(), engine=engine)
    write_override(
        engine, knob="max_position_pct",
        value=8.0, bounds_min=5.0, bounds_max=15.0,
    )
    assert rm._max_position_pct() == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# WheelLane — iv_rank_floor
# ---------------------------------------------------------------------------


def _wheel_cfg(**overrides) -> WheelConfig:
    base = dict(
        enabled=True, vix_floor=10, vix_ceiling=30, sentiment_floor=-1.0,
        iv_rank_floor=30.0, iv_rank_min_history=2,
        delta_target_low=0.20, delta_target_high=0.30,
        dte_min=21, dte_max=45,
        min_premium_abs=0.20, min_annualized_yield=0.12,
        min_open_interest=0,
        liquidity_max_spread_abs=10.0, liquidity_max_spread_rel=1.0,
        unblock_debate_enabled=False,
    )
    base.update(overrides)
    return WheelConfig(**base)


class _StubFinnhub:
    def has_earnings_in_window(self, sym, start, end):
        return False


def _wheel_inputs(*, iv_rank: float | None) -> WheelInputs:
    return WheelInputs(
        symbol="TST", regime="trending_up", vix=20.0,
        sentiment_score=0.0, spot=100.0,
        iv_rank=iv_rank, finnhub=_StubFinnhub(),
        today=dt.date(2026, 5, 2), chain=[], cycle=None, cost_basis=None,
    )


def test_wheel_lane_uses_iv_rank_override(engine):
    """Override at iv_rank=15 should let an iv_rank=20 chain through that
    the static floor=30 would have rejected."""
    cfg = _wheel_cfg(iv_rank_floor=30.0)
    lane = WheelLane(cfg, engine=engine)
    # No override → static 30 → iv_rank=20 is rejected
    reason = lane.passes_preflight(_wheel_inputs(iv_rank=20.0))
    assert reason is not None and "iv_rank" in reason
    # Apply override at 15 → iv_rank=20 now passes
    write_override(
        engine, knob="iv_rank_floor",
        value=15.0, bounds_min=10.0, bounds_max=50.0,
    )
    reason = lane.passes_preflight(_wheel_inputs(iv_rank=20.0))
    assert reason is None  # all gates pass


def test_wheel_lane_falls_back_to_static_without_engine():
    cfg = _wheel_cfg(iv_rank_floor=30.0)
    lane = WheelLane(cfg)  # no engine
    reason = lane.passes_preflight(_wheel_inputs(iv_rank=20.0))
    assert reason is not None and "iv_rank" in reason


# ---------------------------------------------------------------------------
# pick_csp_contract — min_premium_abs / min_annualized_yield
# ---------------------------------------------------------------------------


def _make_contract(*, bid=0.50, strike=100.0, dte_days=30) -> ChainContract:
    today = dt.date(2026, 5, 2)
    expiry = today + dt.timedelta(days=dte_days)
    return ChainContract(
        contract_symbol="TST_PUT",
        underlying="TST", expiration=expiry, kind="P", strike=strike,
        bid=bid, ask=bid + 0.05, last=bid,
        volume=100, open_interest=100, implied_volatility=0.30,
        delta=-0.25,
    )


def test_pick_csp_uses_premium_override(engine):
    """Static floor=0.50; override drops to 0.20; a contract bidding 0.30
    that was rejected before should now be picked."""
    cfg = _wheel_cfg(min_premium_abs=0.50, min_annualized_yield=0.0)
    contract = _make_contract(bid=0.30)
    today = dt.date(2026, 5, 2)
    # No override → rejected
    pick = pick_csp_contract([contract], cfg=cfg, today=today, engine=engine)
    assert pick is None
    # Override the floor down → accepted
    write_override(
        engine, knob="min_premium_abs",
        value=0.20, bounds_min=0.10, bounds_max=1.0,
    )
    pick = pick_csp_contract([contract], cfg=cfg, today=today, engine=engine)
    assert pick is not None
    assert pick.contract_symbol == "TST_PUT"


def test_pick_csp_falls_back_when_no_engine():
    cfg = _wheel_cfg(min_premium_abs=0.50, min_annualized_yield=0.0)
    contract = _make_contract(bid=0.30)
    today = dt.date(2026, 5, 2)
    pick = pick_csp_contract([contract], cfg=cfg, today=today)
    assert pick is None  # static floor still in effect


def test_pick_csp_uses_yield_override(engine):
    """Override on min_annualized_yield works the same way."""
    cfg = _wheel_cfg(min_premium_abs=0.10, min_annualized_yield=0.50)
    # bid=0.50 strike=100 dte=30 → yield ≈ 0.50/100 * 365/30 ≈ 0.061
    contract = _make_contract(bid=0.50, strike=100.0, dte_days=30)
    today = dt.date(2026, 5, 2)
    # Static floor too high → rejected
    assert pick_csp_contract([contract], cfg=cfg, today=today, engine=engine) is None
    # Override floor down → accepted
    write_override(
        engine, knob="min_annualized_yield",
        value=0.05, bounds_min=0.05, bounds_max=0.25,
    )
    pick = pick_csp_contract([contract], cfg=cfg, today=today, engine=engine)
    assert pick is not None


# ---------------------------------------------------------------------------
# Override consultation never crashes when engine has issues.
# ---------------------------------------------------------------------------


def test_pick_csp_engine_none_safe():
    cfg = _wheel_cfg(min_premium_abs=0.20, min_annualized_yield=0.0)
    contract = _make_contract(bid=0.50)
    today = dt.date(2026, 5, 2)
    pick = pick_csp_contract([contract], cfg=cfg, today=today, engine=None)
    assert pick is not None  # static path works fine
