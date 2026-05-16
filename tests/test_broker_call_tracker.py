"""WS5b — broker_call_tracker rolling window."""
from __future__ import annotations

from trading_bot.risk import broker_call_tracker
from trading_bot.risk.kill_switches import detect_broker_api_error_rate


def setup_function() -> None:
    broker_call_tracker.reset()


def test_success_only_no_breach() -> None:
    base = 1000.0
    for i in range(20):
        broker_call_tracker.record_success(now=base + i)
    summary = broker_call_tracker.summarize_window(
        window_seconds=300, now=base + 25,
    )
    assert summary.total == 20
    assert summary.errors == 0
    assert summary.error_rate_pct == 0.0
    assert detect_broker_api_error_rate(
        error_count=summary.errors, total_count=summary.total,
        threshold_pct=5.0,
    ) is None


def test_high_error_rate_fires_kill() -> None:
    base = 1000.0
    for i in range(10):
        broker_call_tracker.record_error(now=base + i)
    for i in range(10):
        broker_call_tracker.record_success(now=base + 10 + i)
    summary = broker_call_tracker.summarize_window(
        window_seconds=300, now=base + 25,
    )
    assert summary.errors == 10 and summary.total == 20
    kill = detect_broker_api_error_rate(
        error_count=summary.errors, total_count=summary.total,
        threshold_pct=5.0,
    )
    assert kill is not None
    assert "broker_api_error_rate" in kill.detector


def test_window_prunes_old_events() -> None:
    base = 1000.0
    broker_call_tracker.record_error(now=base)
    broker_call_tracker.record_error(now=base + 1)
    # 1000s later, window of 300s should drop both.
    summary = broker_call_tracker.summarize_window(
        window_seconds=300, now=base + 1000,
    )
    assert summary.total == 0
