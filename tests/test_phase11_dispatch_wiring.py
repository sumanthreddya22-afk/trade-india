"""Phase 11 — daemon wiring of data-driven discovery into the
dispatch loop and feature_snapshot persistence per decision."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from trading_bot.daemon import strategy_dispatch
from trading_bot.daemon.jobs import DaemonContext
from trading_bot.ingest.universe import AssetRecord
from trading_bot.ledger import connect_writer, create_ledger
from trading_bot.research.historical_bars import (
    DailyBar, adv_provider, ensure_schema, open_store, upsert_bars,
)
from trading_bot.strategies.dual_momentum_v1 import runner as dm_runner


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    p = tmp_path / "ledger.db"
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def test_daemon_context_carries_providers() -> None:
    """The dispatch layer expects asset_fetcher + volume_provider on
    the context so other strategies can adopt the pattern without
    re-plumbing the daemon."""
    ctx = DaemonContext()
    assert ctx.asset_fetcher is None
    assert ctx.volume_provider is None
    ctx.asset_fetcher = lambda cls: []
    ctx.volume_provider = lambda s: None
    assert ctx.asset_fetcher("us_equity") == []
    assert ctx.volume_provider("SPY") is None


def test_runner_extras_passes_only_supported_kwargs() -> None:
    """``_runner_extras`` introspects the runner signature so other
    strategies that don't take asset_fetcher continue to work."""
    def runner_a(*, decision_date, positions_fetcher=None,
                 account_fetcher=None):
        return None

    def runner_b(*, decision_date, positions_fetcher=None,
                 account_fetcher=None, asset_fetcher=None,
                 volume_provider=None):
        return None

    ctx = SimpleNamespace(
        positions_fetcher=lambda: [],
        account_fetcher=lambda: {},
        asset_fetcher=lambda cls: [],
        volume_provider=lambda s: 1.0,
    )
    extras_a = strategy_dispatch._runner_extras(runner_a, ctx)
    assert set(extras_a) == {"positions_fetcher", "account_fetcher"}

    extras_b = strategy_dispatch._runner_extras(runner_b, ctx)
    assert set(extras_b) == {
        "positions_fetcher", "account_fetcher",
        "asset_fetcher", "volume_provider",
    }


def test_feature_snapshot_written_when_decision_has_universe(ledger) -> None:
    """A decision with a universe_payload triggers an idempotent
    feature_snapshot insert. The snapshot_id is returned so the
    strategy_decision row can reference it."""
    ctx = DaemonContext(ledger_db=ledger)
    decision = SimpleNamespace(
        intents=[],
        target_weights={},
        universe=("SPY", "TLT"),
        universe_payload={
            "rule_name": "dual_momentum_v1.default",
            "rule_hash": "abc123",
            "decision_date": "2026-05-15",
            "symbols": ["SPY", "TLT"],
        },
    )
    sid = strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "DUAL_MOMENTUM_v1", decision,
    )
    assert sid.startswith("feat:")

    conn = sqlite3.connect(str(ledger))
    try:
        cur = conn.execute(
            "SELECT snapshot_id, strategy_id, universe_json "
            "FROM feature_snapshot WHERE snapshot_id=?", (sid,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[1] == "DUAL_MOMENTUM_v1"
    assert "SPY" in row[2]

    # Idempotent: a second call returns the same id and writes no new row.
    sid_again = strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "DUAL_MOMENTUM_v1", decision,
    )
    assert sid_again == sid
    conn = sqlite3.connect(str(ledger))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM feature_snapshot WHERE snapshot_id=?",
            (sid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_feature_snapshot_skipped_when_payload_empty(ledger) -> None:
    """Older runners (etf_momentum, etc.) don't expose a
    universe_payload — they currently still carry hardcoded UNIVERSE
    constants. The dispatch must not crash for them; it just skips
    the snapshot write and returns ``""``."""
    ctx = DaemonContext(ledger_db=ledger)
    decision = SimpleNamespace(
        intents=[], target_weights={}, universe_payload={},
    )
    assert strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "ETF_MOMENTUM_v1", decision,
    ) == ""
    decision_no_attr = SimpleNamespace(intents=[], target_weights={})
    assert strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "ETF_MOMENTUM_v1", decision_no_attr,
    ) == ""


def test_skip_row_links_to_feature_snapshot_when_present(ledger) -> None:
    """When a decision skipped but did resolve a universe, the
    strategy_decision skip row must reference the snapshot — that's
    the reproducibility anchor for a backtest replay."""
    ctx = DaemonContext(ledger_db=ledger)
    decision = SimpleNamespace(
        intents=[], target_weights={"SPY": 1.0},
        signal=None, universe=("SPY",),
        universe_payload={"rule_name": "test", "symbols": ["SPY"]},
    )
    sid = strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "DUAL_MOMENTUM_v1", decision,
    )
    strategy_dispatch._record_skip(
        ctx, strategy_id="DUAL_MOMENTUM_v1", strategy_ver=1,
        decision=decision, decision_date=dt.date(2026, 5, 15),
        feature_snapshot_id=sid,
    )

    conn = sqlite3.connect(str(ledger))
    try:
        row = conn.execute(
            "SELECT feature_snapshot_id FROM strategy_decision "
            "WHERE strategy_id=?", ("DUAL_MOMENTUM_v1",),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == sid


def test_runner_returns_universe_payload_in_decision(tmp_path) -> None:
    """The runner must propagate the universe_payload through to the
    StrategyDecision so dispatch can hand it to feature_snapshot. This
    used to be lost on the success path."""
    # Seed historical bars for SPY + TLT so the runner reaches the
    # signal step rather than short-circuiting on missing data.
    hist_db = tmp_path / "hist.db"
    conn = open_store(hist_db)
    base = dt.date(2026, 5, 1)
    bars = []
    for sym in ("SPY", "TLT"):
        for i in range(400):
            d = base - dt.timedelta(days=i)
            bars.append(DailyBar(
                symbol=sym, bar_date=d, open=100, high=101, low=99,
                close=100 + (1 if sym == "SPY" else 0),
                volume=1_000_000,
            ))
    upsert_bars(conn, bars)
    conn.close()

    records = [
        AssetRecord("SPY", "us_equity", True, True, 70e9, attributes=("ETF",)),
        AssetRecord("TLT", "us_equity", True, True, 2e9, attributes=("ETF",)),
    ]
    out = dm_runner.evaluate_strategy(
        historical_db=hist_db, decision_date=dt.date(2026, 5, 15),
        asset_fetcher=lambda cls: records,
        positions_fetcher=lambda: [],
        account_fetcher=lambda: {"equity": 10_000.0, "cash": 10_000.0,
                                  "buying_power": 10_000.0},
    )
    assert out.universe == ("SPY", "TLT")
    assert out.universe_payload["rule_name"] == "dual_momentum_v1.default"
    assert out.universe_payload["rule_hash"] != "fallback:static"


def test_adv_provider_reads_historical_bars(tmp_path) -> None:
    """The volume_provider helper reads close × volume from the
    historical-bars store. < 5 rows → None (insufficient signal)."""
    hist_db = tmp_path / "hist.db"
    conn = open_store(hist_db)
    base = dt.date(2026, 5, 15)
    bars = [
        DailyBar(symbol="SPY", bar_date=base - dt.timedelta(days=i),
                 open=400, high=401, low=399, close=400,
                 volume=70_000_000)
        for i in range(10)
    ]
    bars.append(DailyBar(symbol="THIN", bar_date=base, open=10, high=10,
                         low=10, close=10, volume=1_000))
    upsert_bars(conn, bars)
    conn.close()

    provider = adv_provider(hist_db, as_of=base)
    spy_adv = provider("SPY")
    assert spy_adv is not None
    assert abs(spy_adv - 400 * 70_000_000) < 1.0
    # Symbol with one row → below threshold → None.
    assert provider("THIN") is None
    # Unknown symbol → None.
    assert provider("DOESNTEXIST") is None


def test_adv_provider_returns_none_when_db_missing(tmp_path) -> None:
    provider = adv_provider(tmp_path / "absent.db")
    assert provider("SPY") is None
