"""Tests for the EOD session summary — what went well / wrong / improve."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from trading_bot.session_summary import review_session, SessionReview


class _Ctx:
    """Minimal DigestContext stand-in with only the fields review_session reads."""
    def __init__(self, **kwargs):
        # Defaults match a calm, no-trade day
        self.date = dt.date.today()
        self.starting_equity = Decimal("100000")
        self.ending_equity = Decimal("100000")
        self.realized_pnl = Decimal("0")
        self.unrealized_pnl = Decimal("0")
        self.regime = "trending_up"
        self.daily_loss_cap_pct = 2.0
        self.weekly_loss_cap_pct = 5.0
        self.daily_loss_pct = 0.0
        self.weekly_loss_pct = 0.0
        self.drawdown_pct = 0.0
        self.drawdown_cap_pct = 20.0
        self.vix = 18.0
        self.vol_threshold_pct = 22.0
        self.trades = []
        self.errors = []
        self.daemon_blips = 0
        self.schedule_audit_warnings = []
        self.closed_trades_7d = []
        self.wheel_open_cycles = []
        self.wheel_pnl_mtd = Decimal("0")
        self.wheel_collateral_pct = 0.0
        self.sentiment_scores = []
        self.watchlist_movers = []
        self.pending_promotions = []
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_winning_day_is_in_went_well():
    r = review_session(_Ctx(realized_pnl=Decimal("750")))
    assert any("positive" in s.lower() for s in r.went_well)


def test_losing_day_is_in_went_wrong():
    r = review_session(_Ctx(realized_pnl=Decimal("-750")))
    assert any("negative" in s.lower() for s in r.went_wrong)


def test_zero_trades_flagged_as_improvement():
    r = review_session(_Ctx(trades=[]))
    assert any("zero trades" in s.lower() for s in r.improvements)


def test_runtime_errors_in_went_wrong():
    r = review_session(_Ctx(errors=["error1", "error2"]))
    assert any("runtime errors" in s.lower() for s in r.went_wrong)


def test_no_errors_in_went_well():
    r = review_session(_Ctx(errors=[]))
    assert any("zero runtime errors" in s.lower() for s in r.went_well)


def test_daily_loss_75pct_of_cap_flags_warning():
    """daily_loss_pct = -1.6%, cap = 2% → 80% of limit → flag."""
    r = review_session(_Ctx(daily_loss_pct=-1.6, daily_loss_cap_pct=2.0))
    assert any("daily loss" in s.lower() and "cap" in s.lower()
               for s in r.went_wrong)


def test_low_win_rate_in_went_wrong():
    closed = [{"pnl": -50}, {"pnl": -30}, {"pnl": 20}, {"pnl": -10}, {"pnl": 5}]
    r = review_session(_Ctx(closed_trades_7d=closed))
    # 2/5 = 40% — not below the strict <40% threshold; bump to 1/5 = 20%
    closed = [{"pnl": -50}, {"pnl": -30}, {"pnl": -20}, {"pnl": -10}, {"pnl": 5}]
    r = review_session(_Ctx(closed_trades_7d=closed))
    assert any("win rate" in s.lower() for s in r.went_wrong)


def test_high_win_rate_in_went_well():
    closed = [{"pnl": 50}, {"pnl": 30}, {"pnl": 20}, {"pnl": -10}, {"pnl": 25}]
    r = review_session(_Ctx(closed_trades_7d=closed))
    assert any("win rate" in s.lower() for s in r.went_well)


def test_risk_off_regime_flags_improvement():
    r = review_session(_Ctx(regime="risk_off"))
    assert any("risk-off" in s.lower() for s in r.improvements)


def test_high_vix_flags_improvement():
    r = review_session(_Ctx(vix=28.0, vol_threshold_pct=22.0))
    assert any("vix" in s.lower() and "above" in s.lower()
               for s in r.improvements)


def test_low_vix_flags_thin_premiums():
    r = review_session(_Ctx(vix=11.5))
    assert any("vix" in s.lower() and ("thin" in s.lower() or "low" in s.lower())
               for s in r.improvements)


def test_wheel_collateral_high_flags_improvement():
    r = review_session(_Ctx(wheel_collateral_pct=19.5))
    assert any("collateral" in s.lower() for s in r.improvements)


def test_returns_session_review_dataclass():
    r = review_session(_Ctx())
    assert isinstance(r, SessionReview)
    assert isinstance(r.went_well, list)
    assert isinstance(r.went_wrong, list)
    assert isinstance(r.improvements, list)


def test_safety_always_at_least_one_well_and_one_improve():
    """Even on a "nothing happened" day, return at least one of each."""
    r = review_session(_Ctx())
    assert len(r.went_well) >= 1
    assert len(r.improvements) >= 1
