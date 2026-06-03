"""NIFTY_GAP_v1 — signal logic + runner research_only contract."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.strategies import nifty_gap_v1
from trading_bot.strategies.nifty_gap_v1 import runner, signal
from trading_bot.strategies.nifty_gap_v1.signal import (
    DEFAULT_PARAMS, GapSignal, compute_gap_signal,
)


# ---------------------------------------------------------------------------
# Signal logic
# ---------------------------------------------------------------------------


def test_gap_below_threshold_returns_flat() -> None:
    # 100 → 100.3 is +0.3% gap, below the 0.5% default threshold.
    sig = compute_gap_signal(prior_close=100.0, today_open=100.3)
    assert sig.action == "flat"
    assert sig.gap_pct == pytest.approx(0.3, abs=1e-9)


def test_gap_up_at_threshold_triggers_fade_up() -> None:
    sig = compute_gap_signal(prior_close=100.0, today_open=100.5)
    assert sig.action == "fade_up"
    assert sig.gap_pct == pytest.approx(0.5, abs=1e-9)


def test_gap_down_at_threshold_triggers_fade_down() -> None:
    sig = compute_gap_signal(prior_close=100.0, today_open=99.5)
    assert sig.action == "fade_down"
    assert sig.gap_pct == pytest.approx(-0.5, abs=1e-9)


def test_large_gap_up_triggers_fade_up() -> None:
    sig = compute_gap_signal(prior_close=200.0, today_open=204.0)
    assert sig.action == "fade_up"
    assert sig.gap_pct == pytest.approx(2.0, abs=1e-9)


def test_large_gap_down_triggers_fade_down() -> None:
    sig = compute_gap_signal(prior_close=200.0, today_open=190.0)
    assert sig.action == "fade_down"
    assert sig.gap_pct == pytest.approx(-5.0, abs=1e-9)


def test_threshold_override_lifts_low_gap_into_flat() -> None:
    """A 0.4% gap fires at default 0.5% threshold = flat. A 1.0%
    threshold makes the same gap still flat."""
    sig = compute_gap_signal(
        prior_close=100.0, today_open=100.4, gap_threshold_pct=1.0,
    )
    assert sig.action == "flat"


def test_threshold_override_lowers_threshold() -> None:
    """A 0.3% gap is flat at default 0.5% but fades at 0.25% threshold."""
    sig = compute_gap_signal(
        prior_close=100.0, today_open=100.3, gap_threshold_pct=0.25,
    )
    assert sig.action == "fade_up"


def test_zero_or_negative_prior_close_returns_flat() -> None:
    """Defensive: bad input must not divide by zero."""
    sig = compute_gap_signal(prior_close=0.0, today_open=100.0)
    assert sig.action == "flat"
    assert sig.gap_pct == 0.0

    sig2 = compute_gap_signal(prior_close=-50.0, today_open=100.0)
    assert sig2.action == "flat"


def test_no_gap_returns_flat() -> None:
    sig = compute_gap_signal(prior_close=100.0, today_open=100.0)
    assert sig.action == "flat"
    assert sig.gap_pct == 0.0


# ---------------------------------------------------------------------------
# Threshold grid for the mutation engine
# ---------------------------------------------------------------------------


def test_default_param_grid_is_monotone_and_brackets_default() -> None:
    grid = DEFAULT_PARAMS["_gap_threshold_pct_grid"]
    assert list(grid) == sorted(grid), "grid must be ascending"
    assert DEFAULT_PARAMS["gap_threshold_pct"] in grid


# ---------------------------------------------------------------------------
# Runner contract (research_only — no intents emitted yet)
# ---------------------------------------------------------------------------


def test_evaluate_strategy_emits_no_intents() -> None:
    decision = runner.evaluate_strategy(decision_date=dt.date(2026, 6, 3))
    assert decision.intents == []


def test_evaluate_strategy_reports_universe_and_status() -> None:
    decision = runner.evaluate_strategy(decision_date=dt.date(2026, 6, 3))
    assert decision.universe == ("NIFTYBEES",)
    assert decision.universe_payload["_status"] == "research_only"


def test_should_rebalance_today_is_daily() -> None:
    today = dt.date(2026, 6, 3)
    assert runner.should_rebalance_today(today, last_date=None) is True
    assert runner.should_rebalance_today(today, last_date=today - dt.timedelta(days=1)) is True
    # Same day = already done.
    assert runner.should_rebalance_today(today, last_date=today) is False


def test_package_exports_match_signal_module() -> None:
    assert nifty_gap_v1.STRATEGY_ID == "NIFTY_GAP_v1"
    assert nifty_gap_v1.UNIVERSE == ("NIFTYBEES",)
    assert nifty_gap_v1.compute_gap_signal is signal.compute_gap_signal
