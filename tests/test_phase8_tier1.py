"""Tier-1 harness end-to-end with synthetic data.

We seed historical bars in a tmp DB, run the harness against the real
ETF Momentum signal module, and assert the artifact lands.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from trading_bot.research.historical_bars import DailyBar, open_store, upsert_bars
from trading_bot.research.tier1 import run_tier1
from trading_bot.strategies import etf_momentum_v1


def _seed_universe_bars(db: Path, *, days: int = 365 * 6) -> None:
    """5 years of synthetic data for the seed universe, deterministic."""
    conn = open_store(db)
    try:
        bars = []
        end = dt.date(2024, 12, 31)
        for i, sym in enumerate(etf_momentum_v1.UNIVERSE):
            slope = 0.0003 * (1 + i % 5)
            jitter = 0.001 * (i % 3 + 1)
            price = 100.0
            for d in range(days):
                date = end - dt.timedelta(days=(days - 1 - d))
                # Predictable wave so SHARPE isn't infinite (need variance).
                noise = jitter * ((d % 7) - 3) / 10.0
                price *= (1 + slope + noise)
                bars.append(DailyBar(
                    symbol=sym, bar_date=date,
                    open=price, high=price * 1.005, low=price * 0.995,
                    close=price, volume=1_000_000,
                ))
        upsert_bars(conn, bars)
    finally:
        conn.close()


@pytest.fixture()
def ledger(tmp_path):
    p = tmp_path / "ledger.db"
    from trading_bot.ledger import connect_writer, create_ledger
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def test_tier1_writes_artifact(tmp_path, ledger):
    hist = tmp_path / "historical_bars.db"
    # 7y of data → 4.9y in-sample after 30% holdout → 5 folds at 24/6.
    _seed_universe_bars(hist, days=365 * 7)

    cost_lock = json.loads(
        (Path(__file__).resolve().parent.parent / "policy" / "cost_model.lock").read_text()
    )
    val_lock = json.loads(
        (Path(__file__).resolve().parent.parent / "policy" / "validation_policy.lock").read_text()
    )

    result = run_tier1(
        strategy_id="ETF_MOMENTUM_v1", strategy_ver=1,
        signal_module=etf_momentum_v1,
        historical_db=hist, cost_model_lock=cost_lock,
        validation_policy_lock=val_lock,
        start=dt.date(2018, 1, 1), end=dt.date(2024, 12, 31),
        ledger_db=ledger,
        # Smaller variant grid for speed
        variant_keys=("top_n",),
        variant_values={"top_n": (2, 3, 4)},
    )

    assert result.artifact_id   # nonempty string
    assert result.n_variants == 3
    # Walk-forward folds must be >= 5 (from build_folds default min_folds).
    assert result.n_walk_forward_folds >= 5
    # Pessimistic lens result has trades + final equity.
    assert result.pessimistic_result.lens == "pessimistic"
    assert result.pessimistic_result.starting_equity == 100_000.0

    # Artifact must be present in the ledger.
    import sqlite3
    conn = sqlite3.connect(str(ledger))
    try:
        cur = conn.execute(
            "SELECT artifact_id, strategy_id, tier, pass FROM validation_artifact"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == result.artifact_id
    assert rows[0][1] == "ETF_MOMENTUM_v1"
    assert rows[0][2] == "research_candidate"
