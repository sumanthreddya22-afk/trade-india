"""Intel snapshot wiring — snapshot_payload + dispatch integration."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_bot.daemon import strategy_dispatch
from trading_bot.daemon.jobs import DaemonContext
from trading_bot.ingest.intel import (
    IntelRecord, IntelUnavailable, snapshot_payload,
)
from trading_bot.ledger import connect_writer, create_ledger


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    p = tmp_path / "ledger.db"
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


class _GoodFeed:
    feed_id = "good_v1"
    def fetch(self, decision_date):
        return {
            "X": IntelRecord(
                feed_id=self.feed_id, series_id="X", value=42.0,
                unit="count", source_ts="2026-05-15",
                fetched_ts="2026-05-15T12:00:00+00:00",
            ),
        }


class _BadFeed:
    feed_id = "bad_v1"
    def fetch(self, decision_date):
        raise IntelUnavailable("upstream down")


def test_snapshot_payload_carries_records_and_errors_per_feed() -> None:
    """Every feed contributes a slot keyed by feed_id. A failing feed
    surfaces an ``_error`` slot instead of being silently dropped."""
    out = snapshot_payload(
        [_GoodFeed(), _BadFeed()], dt.date(2026, 5, 15),
    )
    assert "good_v1" in out
    assert out["good_v1"]["X"]["value"] == 42.0
    assert out["good_v1"]["X"]["source_hash"]
    assert out["bad_v1"] == {"_error": "upstream down"}


def test_snapshot_payload_empty_when_no_feeds() -> None:
    assert snapshot_payload([], dt.date(2026, 5, 15)) == {}


def test_dispatch_includes_intel_in_feature_snapshot(ledger) -> None:
    """When ctx carries intel_feeds, the snapshot row's intel_json
    contains the fetched payload — anchors reproducibility for replay."""
    ctx = DaemonContext(ledger_db=ledger, intel_feeds=[_GoodFeed()])
    decision = SimpleNamespace(
        intents=[], target_weights={},
        universe=("SPY", "TLT"),
        universe_payload={
            "rule_name": "test", "rule_hash": "abc",
            "decision_date": "2026-05-15", "symbols": ["SPY", "TLT"],
        },
        decision_date=dt.date(2026, 5, 15),
    )
    sid = strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "DUAL_MOMENTUM_v1", decision,
    )
    assert sid.startswith("feat:")

    conn = sqlite3.connect(str(ledger))
    try:
        cur = conn.execute(
            "SELECT intel_json FROM feature_snapshot WHERE snapshot_id=?",
            (sid,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    intel = json.loads(row[0])
    assert "good_v1" in intel
    assert intel["good_v1"]["X"]["value"] == 42.0


def test_dispatch_isolates_failing_feed_into_intel_block(ledger) -> None:
    """One feed failing must NOT block the snapshot write — the bad
    feed's slot just carries ``_error`` and the good feed's data is
    preserved."""
    ctx = DaemonContext(
        ledger_db=ledger, intel_feeds=[_GoodFeed(), _BadFeed()],
    )
    decision = SimpleNamespace(
        intents=[], target_weights={},
        universe=("SPY",),
        universe_payload={"rule_name": "test", "symbols": ["SPY"]},
        decision_date=dt.date(2026, 5, 15),
    )
    sid = strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "DUAL_MOMENTUM_v1", decision,
    )
    conn = sqlite3.connect(str(ledger))
    try:
        intel_json = conn.execute(
            "SELECT intel_json FROM feature_snapshot WHERE snapshot_id=?",
            (sid,),
        ).fetchone()[0]
    finally:
        conn.close()
    intel = json.loads(intel_json)
    assert intel["good_v1"]["X"]["value"] == 42.0
    assert "_error" in intel["bad_v1"]


def test_dispatch_with_no_feeds_writes_empty_intel(ledger) -> None:
    """Default DaemonContext has no intel_feeds — snapshot still writes
    with intel={}. Backward-compatible with the Phase 10 schema."""
    ctx = DaemonContext(ledger_db=ledger)
    decision = SimpleNamespace(
        intents=[], target_weights={},
        universe_payload={"rule_name": "t", "symbols": ["SPY"]},
        decision_date=dt.date(2026, 5, 15),
    )
    sid = strategy_dispatch._maybe_write_feature_snapshot(
        ctx, "DUAL_MOMENTUM_v1", decision,
    )
    conn = sqlite3.connect(str(ledger))
    try:
        intel_json = conn.execute(
            "SELECT intel_json FROM feature_snapshot WHERE snapshot_id=?",
            (sid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(intel_json) == {}
