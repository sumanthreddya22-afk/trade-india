"""Phase A — v3 strategy runners (fallback path + daily cadence)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trading_bot.strategies import (
    crypto_momentum_v3, dual_momentum_v3, etf_momentum_v3,
)


def test_etf_v3_falls_back_when_no_fetcher() -> None:
    result = etf_momentum_v3.evaluate_strategy(
        decision_date=dt.date(2026, 5, 15),
    )
    # Fallback symbols populate the universe even without a fetcher.
    assert len(result.universe) > 0
    assert "SPY" in result.universe


def test_etf_v3_daily_cadence_always_rebalances() -> None:
    today = dt.date(2026, 5, 15)
    assert etf_momentum_v3.should_rebalance_today(today, today - dt.timedelta(days=1)) is True
    assert etf_momentum_v3.should_rebalance_today(today, today) is True
    assert etf_momentum_v3.should_rebalance_today(today, None) is True


def test_crypto_v3_runs_24_7() -> None:
    assert crypto_momentum_v3.RUNS_ON_NON_TRADING_DAYS is True


def test_crypto_v3_falls_back() -> None:
    result = crypto_momentum_v3.evaluate_strategy(
        decision_date=dt.date(2026, 5, 15),
    )
    assert "BTC/USD" in result.universe or "ETH/USD" in result.universe


def test_dual_v3_falls_back_with_sleeves() -> None:
    result = dual_momentum_v3.evaluate_strategy(
        decision_date=dt.date(2026, 5, 15),
    )
    # Two sleeves -> at least 1 symbol per sleeve from fallback.
    assert result.universe_payload is not None
    if result.universe_payload.get("sleeves"):
        sleeves = result.universe_payload["sleeves"]
        assert "equity" in sleeves
        assert "treasury" in sleeves


def test_etf_v3_evaluation_handles_missing_historical_db(tmp_path: Path) -> None:
    fake_db = tmp_path / "missing.db"
    result = etf_momentum_v3.evaluate_strategy(
        decision_date=dt.date(2026, 5, 15),
        historical_db=fake_db,
    )
    assert result.intents == []
