"""Backtest engine behaviour tests."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.research.backtest import (
    CostLens, run_backtest, run_three_lens_backtest,
)
from trading_bot.research.historical_bars import DailyBar


def _flat_bars(symbol: str, start: dt.date, n_days: int, close: float = 100.0):
    return [
        DailyBar(symbol=symbol, bar_date=start + dt.timedelta(days=i),
                 open=close, high=close, low=close, close=close, volume=1_000_000)
        for i in range(n_days)
    ]


def _rising_bars(symbol: str, start: dt.date, n_days: int, daily_pct: float):
    bars = []
    price = 100.0
    for i in range(n_days):
        bars.append(DailyBar(
            symbol=symbol, bar_date=start + dt.timedelta(days=i),
            open=price, high=price * 1.01, low=price * 0.99,
            close=price, volume=1_000_000,
        ))
        price *= (1 + daily_pct)
    return bars


def test_no_signal_means_no_trades():
    start = dt.date(2024, 1, 1)
    bars = {"SPY": _flat_bars("SPY", start, 30)}

    def zero_signal(history, date): return {}

    r = run_backtest(
        bars_by_symbol=bars, signal_fn=zero_signal,
        start=start, end=start + dt.timedelta(days=29),
        starting_equity=10_000.0,
        cost_lens=CostLens.raw(),
    )
    assert r.n_trades == 0
    assert r.final_equity == 10_000.0


def test_buy_and_hold_full_weight_recovers_underlying_return():
    start = dt.date(2024, 1, 1)
    daily = 0.001
    n_days = 60
    bars = {"X": _rising_bars("X", start, n_days, daily_pct=daily)}

    def all_in(history, date): return {"X": 1.0}

    r = run_backtest(
        bars_by_symbol=bars, signal_fn=all_in,
        start=start, end=start + dt.timedelta(days=n_days - 1),
        starting_equity=10_000.0,
        cost_lens=CostLens.raw(),
        rebalance_freq="monthly",
    )
    # With raw lens and no fees, we should approximate the underlying
    # return. Allow small tolerance for the 1-day fill lag.
    total_ret = r.final_equity / r.starting_equity - 1.0
    expected = (1 + daily) ** (n_days - 2) - 1.0
    assert abs(total_ret - expected) / expected < 0.05


def test_pessimistic_lens_costs_more_than_raw():
    start = dt.date(2024, 1, 1)
    bars = {"X": _rising_bars("X", start, 60, daily_pct=0.002)}

    def all_in(history, date): return {"X": 1.0}

    raw = run_backtest(
        bars_by_symbol=bars, signal_fn=all_in,
        start=start, end=start + dt.timedelta(days=59),
        starting_equity=10_000.0, cost_lens=CostLens.raw(),
    )
    pessimistic = run_backtest(
        bars_by_symbol=bars, signal_fn=all_in,
        start=start, end=start + dt.timedelta(days=59),
        starting_equity=10_000.0,
        cost_lens=CostLens.pessimistic({
            "stocks": {"extra_slippage_bps": 5,
                       "sec_section_31_rate": 0.0000278,
                       "finra_taf_per_share": 0.000166,
                       "finra_taf_cap_per_trade": 8.30},
        }),
    )
    assert pessimistic.final_equity < raw.final_equity
    assert pessimistic.total_fees > 0


def test_three_lens_shapes():
    start = dt.date(2024, 1, 1)
    bars = {"X": _rising_bars("X", start, 60, daily_pct=0.001)}

    def all_in(history, date): return {"X": 1.0}

    cost_lock = {
        "stocks": {"extra_slippage_bps": 5, "sec_section_31_rate": 0.0000278,
                   "finra_taf_per_share": 0.000166, "finra_taf_cap_per_trade": 8.3},
    }
    results = run_three_lens_backtest(
        bars_by_symbol=bars, signal_fn=all_in,
        start=start, end=start + dt.timedelta(days=59),
        starting_equity=10_000.0, cost_model_lock=cost_lock,
    )
    assert set(results.keys()) == {"raw", "broker_paper", "pessimistic"}
    # Strict ordering only holds if there are at least some trades.
    if results["raw"].n_trades > 0:
        assert results["raw"].final_equity >= results["broker_paper"].final_equity
        assert results["broker_paper"].final_equity >= results["pessimistic"].final_equity


def test_decision_lag_orders_fill_next_bar():
    """A buy decision on day D fills at day D+1's open (decision_lag_days=1)."""
    start = dt.date(2024, 1, 1)
    bars = {"X": _rising_bars("X", start, 30, daily_pct=0.01)}

    # Force a rebalance every day; weight=1.0 on X.
    def all_in(h, d): return {"X": 1.0}

    r = run_backtest(
        bars_by_symbol=bars, signal_fn=all_in,
        start=start, end=start + dt.timedelta(days=29),
        starting_equity=10_000.0, cost_lens=CostLens.raw(),
        rebalance_freq="daily",
    )
    # First trade's fill_date must be at least 1 day after the first
    # trading date.
    assert r.trades
    assert r.trades[0].fill_date > start
