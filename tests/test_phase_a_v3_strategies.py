"""Phase A — v3 strategy runners (fallback path + daily cadence).

Post India migration: fallback universes use NSE ETFs and INR crypto
pairs, NOT US tickers.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trading_bot.strategies import (
    crypto_momentum_v3, dual_momentum_v3, etf_momentum_v3,
)
from trading_bot.strategies.crypto_momentum_v3 import runner as _crypto_v3_runner
from trading_bot.strategies.etf_momentum_v3 import runner as _etf_v3_runner


def test_etf_v3_fallback_constant_is_nse() -> None:
    """The wired fallback symbols (used when no asset_fetcher is
    injected) must be NSE tickers, not US. Tested at the module level
    because evaluate_strategy short-circuits to an empty universe when
    no historical_bars DB exists (verified separately below)."""
    fallback = _etf_v3_runner._FALLBACK_UNIVERSE
    assert "NIFTYBEES" in fallback
    for sym in fallback:
        # No US tickers like SPY/QQQ/IWM should leak through.
        assert sym not in ("SPY", "QQQ", "IWM", "VTI", "DIA")


def test_etf_v3_daily_cadence_always_rebalances() -> None:
    today = dt.date(2026, 5, 15)
    assert etf_momentum_v3.should_rebalance_today(today, today - dt.timedelta(days=1)) is True
    assert etf_momentum_v3.should_rebalance_today(today, today) is True
    assert etf_momentum_v3.should_rebalance_today(today, None) is True


def test_crypto_v3_runs_24_7() -> None:
    assert crypto_momentum_v3.RUNS_ON_NON_TRADING_DAYS is True


def test_crypto_v3_fallback_constant_is_inr() -> None:
    """India migration: crypto fallback is INR-quoted, not USD. Tested
    at module level (evaluate_strategy short-circuits without a
    historical_bars DB)."""
    fallback = _crypto_v3_runner._FALLBACK_UNIVERSE
    assert "BTC/INR" in fallback or "ETH/INR" in fallback
    for sym in fallback:
        assert not sym.endswith("/USD"), f"USD pair {sym!r} leaked into India fallback"


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
