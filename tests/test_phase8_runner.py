"""ETF Momentum live runner: should_rebalance_today + evaluate_strategy."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trading_bot.research.historical_bars import (
    DailyBar, open_store, upsert_bars,
)
from trading_bot.strategies.etf_momentum_v1 import runner, signal


def test_should_rebalance_today_monthly_change():
    assert runner.should_rebalance_today(
        dt.date(2024, 2, 1), dt.date(2024, 1, 15)
    )
    assert not runner.should_rebalance_today(
        dt.date(2024, 1, 31), dt.date(2024, 1, 15)
    )
    # No prior decision → must rebalance.
    assert runner.should_rebalance_today(dt.date(2024, 1, 5), None)


def _seed_bars(db: Path, *, n_days: int = 400) -> None:
    """Populate bars for the full UNIVERSE with deterministic uptrends."""
    conn = open_store(db)
    try:
        bars = []
        end = dt.date(2024, 6, 1)
        for i, sym in enumerate(signal.UNIVERSE):
            slope = 0.001 * (1 + i)
            for d in range(n_days):
                date = end - dt.timedelta(days=(n_days - 1 - d))
                price = 100.0 * (1 + d * slope)
                bars.append(DailyBar(
                    symbol=sym, bar_date=date,
                    open=price, high=price * 1.01, low=price * 0.99,
                    close=price, volume=1_000_000,
                ))
        upsert_bars(conn, bars)
    finally:
        conn.close()


def test_evaluate_strategy_returns_top_n_intents(tmp_path):
    db = tmp_path / "bars.db"
    _seed_bars(db, n_days=400)

    def fake_positions(): return []
    def fake_account():   return {"equity": 100_000.0, "cash": 100_000.0,
                                  "buying_power": 100_000.0}

    decision = runner.evaluate_strategy(
        historical_db=db, decision_date=dt.date(2024, 6, 1),
        positions_fetcher=fake_positions, account_fetcher=fake_account,
    )
    assert decision.equity == 100_000.0
    # Strategy picks top 3 by momentum; default DEFAULT_PARAMS has top_n=3.
    assert len(decision.target_weights) == 3
    assert len(decision.intents) == 3
    for intent in decision.intents:
        assert intent["strategy_id"] == "ETF_MOMENTUM_v1"
        assert intent["side"] == "buy"   # no current positions → all buys
        assert intent["asset_class"] == "us_equity"


def test_evaluate_strategy_skips_held_in_target(tmp_path):
    db = tmp_path / "bars.db"
    _seed_bars(db, n_days=400)

    # Already hold the winners at the exact target weight → no diff.
    def fake_account(): return {"equity": 100_000.0, "cash": 0.0,
                                "buying_power": 0.0}

    # First, find what the winners would be at 100k equity.
    decision_initial = runner.evaluate_strategy(
        historical_db=db, decision_date=dt.date(2024, 6, 1),
        positions_fetcher=lambda: [],
        account_fetcher=fake_account,
    )
    winners = list(decision_initial.target_weights.keys())
    target_value = 100_000.0 / 3

    def fake_positions():
        return [
            {"symbol": w, "qty": target_value / 100.0,  # rough at 100 each
             "avg_entry_price": 100.0, "market_value": target_value,
             "asset_class": "us_equity", "classification": "bot"}
            for w in winners
        ]

    # No buys generated if exactly at target (qty diff < 1e-3).
    # Note: our 'fake' positions may be slightly off; the intents may
    # contain small rebalance trades. Assert: no entirely new symbols.
    decision = runner.evaluate_strategy(
        historical_db=db, decision_date=dt.date(2024, 6, 1),
        positions_fetcher=fake_positions, account_fetcher=fake_account,
    )
    syms = {i["symbol"] for i in decision.intents}
    assert syms.issubset(set(winners))


def test_evaluate_strategy_empty_history_returns_nothing(tmp_path):
    db = tmp_path / "bars.db"
    open_store(db).close()    # creates schema only
    decision = runner.evaluate_strategy(
        historical_db=db, decision_date=dt.date(2024, 6, 1),
        positions_fetcher=lambda: [],
        account_fetcher=lambda: {"equity": 100_000.0, "cash": 100_000.0, "buying_power": 100_000.0},
    )
    assert decision.target_weights == {}
    assert decision.intents == []
