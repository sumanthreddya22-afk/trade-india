"""Walk-forward harness tests."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

from trading_bot.walkforward import (
    _BASELINE_LAB_UNIVERSE,
    _ensure_bars_warmed,
    _lab_universe,
    FoldDefinition,
    default_folds,
    walk_forward_backtest,
)


def test_default_folds_six_with_room():
    """6 folds (12mo train + 3mo test, walking quarterly) need 30 months range.
    Range 2024-01-01 → 2026-07-01 (=30 months) fits exactly 6 folds."""
    start = dt.date(2024, 1, 1)
    end = dt.date(2026, 7, 1)
    folds = default_folds(start=start, end=end, n_folds=6)
    assert len(folds) == 6
    # First fold: 2024-01..2024-12 train, 2025-01..2025-03 test
    assert folds[0].train_start == dt.date(2024, 1, 1)
    assert folds[0].train_end == dt.date(2024, 12, 31)
    assert folds[0].test_start == dt.date(2025, 1, 1)
    assert folds[0].test_end == dt.date(2025, 3, 31)
    # Last fold: train 2025-04..2026-03, test 2026-04..2026-06
    assert folds[5].test_start == dt.date(2026, 4, 1)
    assert folds[5].test_end == dt.date(2026, 6, 30)


def test_default_folds_truncates_when_range_too_short():
    """If range can't accommodate n_folds, return however many fit."""
    start = dt.date(2024, 1, 1)
    end = dt.date(2026, 1, 1)  # only 24 months — fits 4 folds, not 6
    folds = default_folds(start=start, end=end, n_folds=6)
    assert len(folds) == 4
    assert folds[3].test_end == dt.date(2025, 12, 31)


def test_walk_forward_invokes_simulator_per_fold():
    with patch("trading_bot.walkforward._run_simulator") as mock_sim:
        mock_sim.return_value = MagicMock()  # BacktestRunResult stub
        results = walk_forward_backtest(
            template_name="momentum",
            params={"rsi_lower": 55.0, "rsi_upper": 70.0},
            start=dt.date(2024, 1, 1),
            end=dt.date(2026, 7, 1),
            n_folds=3,
        )
    assert len(results) == 3
    assert mock_sim.call_count == 3


def test_walk_forward_passes_params_to_runner():
    captured: list = []

    def _capture(*, template_name, params, fold):
        captured.append((template_name, params, fold))
        return MagicMock()

    with patch("trading_bot.walkforward._run_simulator", side_effect=_capture):
        walk_forward_backtest(
            template_name="momentum",
            params={"rsi_lower": 58.0, "rsi_upper": 68.0},
            start=dt.date(2024, 1, 1),
            end=dt.date(2026, 7, 1),
            n_folds=2,
        )
    assert captured[0][0] == "momentum"
    assert captured[0][1] == {"rsi_lower": 58.0, "rsi_upper": 68.0}
    assert isinstance(captured[0][2], FoldDefinition)


# --- _lab_universe ---------------------------------------------------------


def test_lab_universe_falls_back_when_opportunities_missing(tmp_path):
    fake = tmp_path / "missing.md"
    universe = _lab_universe(opportunities_path=fake)
    assert universe == list(_BASELINE_LAB_UNIVERSE)


def test_lab_universe_reads_top_n_stocks_from_opportunities(tmp_path):
    """Crypto entries are filtered out; only stocks in rank order, capped at N."""
    p = tmp_path / "opp.md"
    p.write_text(
        "# Opportunities\n\n"
        "### 1. NVDA (us_equity)\n- Lanes: momentum\n\n"
        "### 2. ARB/USD (crypto)\n- Lanes: breakout\n\n"
        "### 3. AAPL (us_equity)\n- Lanes: momentum\n\n"
        "### 4. MSFT (us_equity)\n- Lanes: momentum\n"
    )
    universe = _lab_universe(opportunities_path=p)
    assert universe == ["NVDA", "AAPL", "MSFT"]


def test_lab_universe_caps_at_top_n(tmp_path):
    p = tmp_path / "opp.md"
    lines = "\n".join(
        f"### {i}. SYM{i} (us_equity)\n- Lanes: momentum\n"
        for i in range(1, 50)
    )
    p.write_text("# Opportunities\n\n" + lines)
    universe = _lab_universe(opportunities_path=p)
    assert len(universe) == 25  # LAB_UNIVERSE_TOP_N
    assert universe[0] == "SYM1"


def test_lab_universe_empty_stock_list_falls_back(tmp_path):
    """If opportunities.md exists but only contains crypto, fall back to baseline."""
    p = tmp_path / "opp.md"
    p.write_text(
        "# Opportunities\n\n"
        "### 1. BTC/USD (crypto)\n\n"
        "### 2. ETH/USD (crypto)\n"
    )
    universe = _lab_universe(opportunities_path=p)
    assert universe == list(_BASELINE_LAB_UNIVERSE)


# --- _ensure_bars_warmed ---------------------------------------------------


def test_ensure_bars_warmed_skips_when_all_warm():
    bar_store = MagicMock()
    bar_store.is_warm = MagicMock(return_value=True)
    out = _ensure_bars_warmed(
        bar_store, symbols=["A", "B", "C"],
        from_date=dt.date(2024, 1, 1), to_date=dt.date(2024, 6, 1),
    )
    assert out == ["A", "B", "C"]
    assert not bar_store.warm.called


def test_ensure_bars_warmed_calls_warm_for_missing():
    bar_store = MagicMock()
    # B is not warm; A and C are
    bar_store.is_warm = MagicMock(side_effect=lambda s, **_: s != "B")
    bar_store.warm = MagicMock(return_value={"B": 100})
    with (
        patch("trading_bot.config.Settings"),
        patch("trading_bot.market_data.MarketDataClient"),
    ):
        out = _ensure_bars_warmed(
            bar_store, symbols=["A", "B", "C"],
            from_date=dt.date(2024, 1, 1), to_date=dt.date(2024, 6, 1),
        )
    assert "A" in out and "B" in out and "C" in out
    assert bar_store.warm.called
    args, kwargs = bar_store.warm.call_args
    only_arg = args[0] if args else kwargs.get("symbols")
    assert list(only_arg) == ["B"]


def test_ensure_bars_warmed_drops_failed_fetches():
    bar_store = MagicMock()
    bar_store.is_warm = MagicMock(return_value=False)  # nothing cached
    bar_store.warm = MagicMock(return_value={"A": 100, "B": -1, "C": -1})
    with (
        patch("trading_bot.config.Settings"),
        patch("trading_bot.market_data.MarketDataClient"),
    ):
        out = _ensure_bars_warmed(
            bar_store, symbols=["A", "B", "C"],
            from_date=dt.date(2024, 1, 1), to_date=dt.date(2024, 6, 1),
        )
    assert out == ["A"]
