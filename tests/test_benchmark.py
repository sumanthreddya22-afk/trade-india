"""SpyBenchmark fetcher tests."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pandas as pd

from trading_bot.benchmark import SpyBenchmark


def test_returns_dataframe_with_close_column(tmp_path):
    bench = SpyBenchmark(cache_path=tmp_path / "spy.parquet")
    fake_df = pd.DataFrame(
        {"close": [100.0, 101.5, 99.8]},
        index=pd.to_datetime(["2026-04-25", "2026-04-26", "2026-04-27"]),
    )
    with patch.object(SpyBenchmark, "_fetch_alpaca", return_value=fake_df):
        df = bench.get(start=dt.date(2026, 4, 25), end=dt.date(2026, 4, 27))
    assert "close" in df.columns
    assert len(df) == 3


def test_falls_back_to_stooq_on_alpaca_failure(tmp_path):
    bench = SpyBenchmark(cache_path=tmp_path / "spy.parquet")
    fake_df = pd.DataFrame(
        {"close": [100.0]}, index=pd.to_datetime(["2026-04-25"])
    )
    with (
        patch.object(SpyBenchmark, "_fetch_alpaca", side_effect=ConnectionError),
        patch.object(SpyBenchmark, "_fetch_stooq", return_value=fake_df),
    ):
        df = bench.get(start=dt.date(2026, 4, 25), end=dt.date(2026, 4, 25))
    assert len(df) == 1


def test_compute_period_return():
    df = pd.DataFrame(
        {"close": [100.0, 110.0]},
        index=pd.to_datetime(["2026-04-01", "2026-04-30"]),
    )
    ret = SpyBenchmark.period_return(df)
    assert abs(ret - 0.10) < 1e-6


def test_period_return_empty():
    df = pd.DataFrame({"close": []}, index=pd.to_datetime([]))
    assert SpyBenchmark.period_return(df) == 0.0


def test_period_return_single_row():
    df = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2026-04-25"]))
    assert SpyBenchmark.period_return(df) == 0.0
