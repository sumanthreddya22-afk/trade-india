"""Phase 2 — eight kill-switch detectors + the SQLite event table."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.risk.kill_switches import (
    KILL_TYPES, active_kills, clear, detect_broker_api_error_rate,
    detect_clock_skew, detect_data_freshness, detect_intraday_pnl_floor,
    detect_policy_hash_mismatch, detect_recon_mismatch,
    detect_sqlite_integrity, detect_unknown_position,
    ensure_kill_switch_table, fire,
)


@pytest.fixture()
def killtab(ledger_conn):
    ensure_kill_switch_table(ledger_conn)
    return ledger_conn


def test_kill_types_constant_has_eight_detectors_plus_one_operator() -> None:
    # 8 system-fired detectors per Plan v4 §6 + 1 operator-initiated
    # halt (manual_operator_halt, fired via the dashboard / `bot halt`).
    assert len(KILL_TYPES) == 9
    assert "manual_operator_halt" in KILL_TYPES


def test_fire_then_active(killtab) -> None:
    fire(killtab, detector="recon_mismatch", reason="match=0 at eod")
    assert active_kills(killtab) == {"recon_mismatch"}


def test_fire_then_clear_removes_from_active(killtab) -> None:
    fire(killtab, detector="recon_mismatch", reason="x")
    clear(killtab, detector="recon_mismatch", reason="resolved")
    assert active_kills(killtab) == set()


def test_unknown_detector_rejected(killtab) -> None:
    with pytest.raises(ValueError):
        fire(killtab, detector="ghost", reason="x")


def test_detect_recon_mismatch() -> None:
    assert detect_recon_mismatch(latest_match=0, latest_window="eod")
    assert detect_recon_mismatch(latest_match=1, latest_window="eod") is None


def test_detect_unknown_position_old() -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    old = now - dt.timedelta(minutes=30)
    k = detect_unknown_position(
        positions=[{"symbol": "X", "classification": "unknown",
                    "opened_at": old}],
        max_age_minutes=15, now=now,
    )
    assert k is not None
    assert "unknown" in k.reason


def test_detect_unknown_position_recent_skips() -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    recent = now - dt.timedelta(minutes=5)
    k = detect_unknown_position(
        positions=[{"symbol": "X", "classification": "unknown",
                    "opened_at": recent}],
        max_age_minutes=15, now=now,
    )
    assert k is None


def test_detect_data_freshness() -> None:
    now = dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
    fresh = now - dt.timedelta(seconds=30)
    stale = now - dt.timedelta(seconds=400)
    assert detect_data_freshness(
        watermarks={"equity": fresh},
        thresholds_seconds={"equity": 300}, now=now,
    ) is None
    assert detect_data_freshness(
        watermarks={"equity": stale},
        thresholds_seconds={"equity": 300}, now=now,
    ) is not None


def test_detect_policy_hash_mismatch() -> None:
    expected = {"policy/risk_policy.lock": "a" * 64}
    matched = {"policy/risk_policy.lock": "a" * 64}
    mismatch = {"policy/risk_policy.lock": "b" * 64}
    assert detect_policy_hash_mismatch(expected=expected, actual=matched) is None
    k = detect_policy_hash_mismatch(expected=expected, actual=mismatch)
    assert k is not None and "risk_policy" in k.reason


def test_detect_broker_api_error_rate() -> None:
    assert detect_broker_api_error_rate(
        error_count=2, total_count=100, threshold_pct=5.0,
    ) is None
    assert detect_broker_api_error_rate(
        error_count=6, total_count=100, threshold_pct=5.0,
    ) is not None


def test_detect_clock_skew() -> None:
    assert detect_clock_skew(skew_seconds=1.0, threshold_seconds=2.0) is None
    assert detect_clock_skew(skew_seconds=3.0, threshold_seconds=2.0) is not None
    assert detect_clock_skew(skew_seconds=-3.0, threshold_seconds=2.0) is not None


def test_detect_sqlite_integrity() -> None:
    assert detect_sqlite_integrity("ok") is None
    assert detect_sqlite_integrity("malformed disk image") is not None


def test_detect_intraday_pnl_floor() -> None:
    assert detect_intraday_pnl_floor(pnl_pct=-1.0, floor_pct=-1.5) is None
    assert detect_intraday_pnl_floor(pnl_pct=-1.6, floor_pct=-1.5) is not None
