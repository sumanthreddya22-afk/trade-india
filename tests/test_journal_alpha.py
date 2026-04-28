"""Tests for journal_alpha.compute_journal_alpha_vs_spy."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd

from trading_bot.journal_alpha import (
    INSUFFICIENT_TRADES_THRESHOLD,
    compute_journal_alpha_vs_spy,
)


def _fake_closed_trade(*, exit_date, pnl):
    t = MagicMock()
    t.exit_time = dt.datetime.combine(exit_date, dt.time.min, tzinfo=dt.timezone.utc)
    t.realized_pnl = Decimal(str(pnl))
    return t


def test_insufficient_data_when_few_trades(tmp_path):
    """Empty DB → insufficient_data=True, alpha=0."""
    out = compute_journal_alpha_vs_spy(
        closed_trades_db=tmp_path / "missing.db",
    )
    assert out["insufficient_data"] is True
    assert out["n_trades"] == 0
    assert out["alpha_multiplier"] == 0.0


def test_strategy_beats_spy_two_x(tmp_path):
    """Strategy makes 6%, SPY makes 3% → alpha_multiplier = 2.0."""
    fake_trades = [
        _fake_closed_trade(exit_date=dt.date.today() - dt.timedelta(days=i), pnl=120)
        for i in range(INSUFFICIENT_TRADES_THRESHOLD + 5)
    ]
    spy_df = pd.DataFrame(
        {"close": [100.0, 103.0]},
        index=pd.to_datetime(
            [
                (dt.date.today() - dt.timedelta(days=30)).isoformat(),
                dt.date.today().isoformat(),
            ]
        ),
    )
    fake_store = MagicMock()
    fake_store.all.return_value = fake_trades
    bench = MagicMock()
    bench.get.return_value = spy_df

    with patch("trading_bot.reconciliation.ClosedTradeStore", return_value=fake_store):
        # Make the cdb.exists() call succeed
        cdb = tmp_path / "x.db"
        cdb.write_bytes(b"")
        out = compute_journal_alpha_vs_spy(
            closed_trades_db=cdb,
            starting_equity=Decimal("15000"),
            benchmark=bench,
        )
    assert out["insufficient_data"] is False
    # 10 trades * $120 = $1200 realized; / 15000 = 8% strategy return
    # SPY = 3%; alpha = 8/3 ≈ 2.66
    assert out["alpha_multiplier"] > 2.0
    assert out["alpha_multiplier"] < 3.0


def test_spy_flat_clamps_alpha(tmp_path):
    """SPY ≈ 0% → alpha clamped to a sentinel, not infinity."""
    fake_trades = [
        _fake_closed_trade(exit_date=dt.date.today(), pnl=100)
        for _ in range(INSUFFICIENT_TRADES_THRESHOLD + 5)
    ]
    spy_df = pd.DataFrame(
        {"close": [100.0, 100.00001]},
        index=pd.to_datetime(
            [
                (dt.date.today() - dt.timedelta(days=30)).isoformat(),
                dt.date.today().isoformat(),
            ]
        ),
    )
    fake_store = MagicMock()
    fake_store.all.return_value = fake_trades
    bench = MagicMock()
    bench.get.return_value = spy_df

    with patch("trading_bot.reconciliation.ClosedTradeStore", return_value=fake_store):
        cdb = tmp_path / "x.db"
        cdb.write_bytes(b"")
        out = compute_journal_alpha_vs_spy(
            closed_trades_db=cdb, benchmark=bench
        )
    assert out["alpha_multiplier"] > 0.0
    assert out["alpha_multiplier"] <= 100.0


def test_trades_outside_window_excluded(tmp_path):
    """Old trades outside lookback_days don't count."""
    today = dt.date.today()
    fake_trades = [
        _fake_closed_trade(exit_date=today - dt.timedelta(days=i), pnl=100)
        for i in [1, 2, 3, 60, 90, 100]  # last 3 are outside 30-day window
    ]
    fake_store = MagicMock()
    fake_store.all.return_value = fake_trades
    bench = MagicMock()
    bench.get.return_value = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.to_datetime(
            [
                (today - dt.timedelta(days=30)).isoformat(),
                today.isoformat(),
            ]
        ),
    )

    with patch("trading_bot.reconciliation.ClosedTradeStore", return_value=fake_store):
        cdb = tmp_path / "x.db"
        cdb.write_bytes(b"")
        out = compute_journal_alpha_vs_spy(closed_trades_db=cdb, benchmark=bench)
    assert out["n_trades"] == 3  # only the last 3 are in window
