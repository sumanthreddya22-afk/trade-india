"""Risk profile overlay tests — safe / neutral / aggressive."""
from __future__ import annotations

import json

import pytest

from trading_bot.operator import profiles as P


def test_profiles_present():
    assert set(P.PROFILES.keys()) == {"safe", "neutral", "aggressive"}


def test_safe_is_tighter_than_neutral():
    # All numeric *_max_pct fields in safe must be <= neutral (tighter
    # caps). The intraday floor is more negative in neutral, less
    # negative in safe (safe is "less risk allowed").
    for section in ("asset_class", "order", "symbol"):
        for k, v_safe in P.SAFE_OVERLAY.get(section, {}).items():
            v_neu = P.NEUTRAL_OVERLAY.get(section, {}).get(k)
            if v_neu is None:
                continue
            assert v_safe <= v_neu, f"safe.{section}.{k} ({v_safe}) > neutral ({v_neu})"


def test_aggressive_is_looser_than_neutral():
    for section in ("asset_class", "order", "symbol"):
        for k, v_agg in P.AGGRESSIVE_OVERLAY.get(section, {}).items():
            v_neu = P.NEUTRAL_OVERLAY.get(section, {}).get(k)
            if v_neu is None:
                continue
            assert v_agg >= v_neu, f"aggressive.{section}.{k} ({v_agg}) < neutral ({v_neu})"


def test_is_loosening_caps_vs_floors():
    # Cap: higher = looser
    assert P.is_loosening("asset_class.equity_gross_max_pct", 80.0, 90.0)
    assert not P.is_loosening("asset_class.equity_gross_max_pct", 80.0, 60.0)
    # Floor: more negative = looser
    assert P.is_loosening("account.intraday_pnl_floor_pct_of_equity", -1.5, -2.0)
    assert not P.is_loosening("account.intraday_pnl_floor_pct_of_equity", -1.5, -1.0)


def test_diff_profile_reports_direction():
    cur = {
        "account": {"daily_drawdown_pct_of_equity": 1.0},
        "asset_class": {"equity_gross_max_pct": 80.0},
    }
    diffs = P.diff_profile(cur, P.SAFE_OVERLAY)
    # Safe should tighten everything we have a value for.
    by_path = {d["path"]: d for d in diffs}
    assert by_path["account.daily_drawdown_pct_of_equity"]["direction"] == "tighten"
    assert by_path["asset_class.equity_gross_max_pct"]["direction"] == "tighten"


def test_diff_profile_no_changes_when_identical():
    diffs = P.diff_profile(P.NEUTRAL_OVERLAY, P.NEUTRAL_OVERLAY)
    assert diffs == []
