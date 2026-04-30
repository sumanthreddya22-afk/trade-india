"""W2a — Data-quality gates for market bars.

The PDF requires every decision to record `data_quality.{fresh, complete,
aligned, provenance_ok}`. The gate is fail-closed for compliance-relevant
sources but never silently passes when bars are stale or incomplete.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from trading_bot.data_quality import (
    DataProvenance,
    check_bar_freshness,
    check_completeness,
    snapshot_id_for_bars,
)


def _bars(n: int = 30, *, start: str = "2026-04-01", freq: str = "D"):
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": np.arange(n) + 100.0,
         "high": np.arange(n) + 101.0,
         "low": np.arange(n) + 99.0,
         "close": np.arange(n) + 100.0,
         "volume": [1_000_000] * n},
        index=idx,
    )


class TestFreshness:
    def test_recent_bars_pass(self):
        # Last bar is "now" — fresh
        idx = pd.date_range(end=dt.datetime(2026, 4, 29, 16, 0, tzinfo=dt.timezone.utc),
                            periods=30, freq="D", tz="UTC")
        bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}, index=idx)
        ok, reason = check_bar_freshness(
            bars, asset_class="us_equity",
            max_age_hours=48.0,
            now=dt.datetime(2026, 4, 29, 16, 5, tzinfo=dt.timezone.utc),
        )
        assert ok is True
        assert reason == ""

    def test_stale_bars_fail(self):
        idx = pd.date_range(end=dt.datetime(2026, 4, 25, tzinfo=dt.timezone.utc),
                            periods=30, freq="D", tz="UTC")
        bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}, index=idx)
        ok, reason = check_bar_freshness(
            bars, asset_class="us_equity",
            max_age_hours=48.0,
            now=dt.datetime(2026, 4, 29, 16, 0, tzinfo=dt.timezone.utc),
        )
        assert ok is False
        assert "stale" in reason.lower() or "age" in reason.lower()

    def test_empty_bars_fail(self):
        bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        bars.index = pd.DatetimeIndex([], tz="UTC")
        ok, reason = check_bar_freshness(bars, asset_class="us_equity", max_age_hours=48.0)
        assert ok is False
        assert reason  # non-empty reason

    def test_crypto_uses_24x7_clock(self):
        """Crypto trades 24/7 — a Saturday timestamp 2 hours old should be
        fresh (no weekend gap allowance)."""
        idx = pd.date_range(end=dt.datetime(2026, 4, 25, 18, 0, tzinfo=dt.timezone.utc),
                            periods=30, freq="h", tz="UTC")
        bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}, index=idx)
        ok, reason = check_bar_freshness(
            bars, asset_class="crypto",
            max_age_hours=2.0,
            now=dt.datetime(2026, 4, 25, 18, 30, tzinfo=dt.timezone.utc),  # 30 min later
        )
        assert ok is True


class TestCompleteness:
    def test_clean_bars_pass(self):
        bars = _bars()
        ok, reason = check_completeness(bars, max_missing_pct=5.0)
        assert ok is True
        assert reason == ""

    def test_few_missing_passes(self):
        bars = _bars(n=100)
        # 4 NaN closes out of 100 = 4% missing — under 5% threshold
        bars.loc[bars.index[10:14], "close"] = np.nan
        ok, _ = check_completeness(bars, max_missing_pct=5.0)
        assert ok is True

    def test_too_many_missing_fails(self):
        bars = _bars(n=100)
        bars.loc[bars.index[0:10], "close"] = np.nan  # 10% missing
        ok, reason = check_completeness(bars, max_missing_pct=5.0)
        assert ok is False
        assert "missing" in reason.lower() or "complete" in reason.lower()

    def test_missing_volume_does_not_block_equity_decision(self):
        """Volume is sometimes thin/missing on partial bars; we only block
        on missing OHLC."""
        bars = _bars(n=100)
        bars["volume"] = np.nan  # all volume missing
        ok, _ = check_completeness(bars, max_missing_pct=5.0)
        assert ok is True

    def test_missing_required_column_fails(self):
        bars = _bars(n=20).drop(columns=["close"])
        ok, reason = check_completeness(bars, max_missing_pct=5.0)
        assert ok is False
        assert "close" in reason.lower() or "missing" in reason.lower()


class TestSnapshotId:
    def test_id_is_stable_for_same_input(self):
        bars = _bars()
        a = snapshot_id_for_bars("NVDA", bars)
        b = snapshot_id_for_bars("NVDA", bars)
        assert a == b

    def test_id_changes_when_bars_change(self):
        bars1 = _bars()
        bars2 = _bars(n=29)
        assert snapshot_id_for_bars("NVDA", bars1) != snapshot_id_for_bars("NVDA", bars2)

    def test_id_includes_symbol(self):
        bars = _bars()
        a = snapshot_id_for_bars("NVDA", bars)
        b = snapshot_id_for_bars("AAPL", bars)
        assert a != b


class TestProvenance:
    def test_provenance_dataclass(self):
        prov = DataProvenance(
            source="alpaca",
            fetched_at=dt.datetime(2026, 4, 29, 12, 0, tzinfo=dt.timezone.utc),
            snapshot_id="alpaca:NVDA:2026-04-29:abcd1234",
        )
        assert prov.source == "alpaca"
        assert prov.snapshot_id == "alpaca:NVDA:2026-04-29:abcd1234"
