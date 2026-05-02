"""Phase 6 — system_state.build_system_snapshot unit tests.

Each node in the topology has a health rule. We test the four cases
called out in the plan: ok, warn (late), fail (errored or stalled), off
(never seen).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_bot.dashboard.system_state import build_system_snapshot
from trading_bot.dashboard import system_topology as topo


def _create_schema(db: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', "
            "source TEXT NOT NULL DEFAULT '', "
            "process TEXT NOT NULL DEFAULT 'unknown', "
            "created_at DATETIME NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS role_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "role_name TEXT NOT NULL, started_at DATETIME NOT NULL, "
            "finished_at DATETIME, status TEXT NOT NULL, "
            "latency_ms INTEGER, error_text TEXT)"
        )
        conn.commit()


def _insert_role_run(db: str, role: str, ago_s: float, status: str = "ok") -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO role_runs (role_name, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?)",
            (role, ts, ts, status),
        )
        conn.commit()


def _insert_event(db: str, type_: str, ago_s: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events (type, payload, source, process, created_at) "
            "VALUES (?, '{}', '', 'test', ?)",
            (type_, ts),
        )
        conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    p = str(tmp_path / "state.db")
    _create_schema(p)
    return p


class TestBuildSnapshot:
    def test_passive_intake_nodes_marked_off(self, db: str) -> None:
        snap = build_system_snapshot(db)
        for n in topo.NODES:
            if n.passive:
                assert snap[n.id]["health"] == "off"
                assert snap[n.id]["last_activity_label"] == ""

    def test_recent_role_run_is_ok(self, db: str) -> None:
        # stock_scanner has role_name="stock_scanner".
        _insert_role_run(db, "stock_scanner", ago_s=60)
        snap = build_system_snapshot(db)
        assert snap["stock_scanner"]["health"] == "ok"
        assert "ago" in snap["stock_scanner"]["last_activity_label"]

    def test_late_role_run_is_warn(self, db: str) -> None:
        _insert_role_run(db, "wheel_scan", ago_s=45 * 60)  # 45 min — between OK and WARN cutoff
        snap = build_system_snapshot(db)
        assert snap["wheel_runner"]["health"] == "warn"

    def test_errored_role_run_is_fail(self, db: str) -> None:
        _insert_role_run(db, "reconciler", ago_s=120, status="error")
        snap = build_system_snapshot(db)
        assert snap["reconciler"]["health"] == "fail"

    def test_event_only_node_uses_event_timestamp(self, db: str) -> None:
        # alpaca_trade_stream has no role_name, only subscribes to order.*
        _insert_event(db, "order.filled", ago_s=10)
        snap = build_system_snapshot(db)
        assert snap["alpaca_trade_stream"]["health"] == "ok"

    def test_no_signal_means_off(self, db: str) -> None:
        # No role rows, no events — every active node should be off.
        snap = build_system_snapshot(db)
        # decisions_store is non-passive but has no role and no recent
        # events on a fresh DB.
        assert snap["decisions_store"]["health"] == "off"

    def test_event_overrides_old_role_run(self, db: str) -> None:
        # An old role run + a recent event for the same node — event wins.
        _insert_role_run(db, "stock_scanner", ago_s=2 * 3600)  # 2h ago
        _insert_event(db, "scan.completed", ago_s=10)
        snap = build_system_snapshot(db)
        assert snap["stock_scanner"]["health"] == "ok"

    def test_missing_db_returns_all_off(self, tmp_path: Path) -> None:
        snap = build_system_snapshot(tmp_path / "does-not-exist.db")
        assert all(info["health"] == "off" for info in snap.values())
        assert all(info["last_activity_label"] == "" for info in snap.values())


class TestTopologyShape:
    def test_every_zone_has_at_least_one_node(self) -> None:
        from collections import Counter
        counts = Counter(n.zone for n in topo.NODES)
        for zone_id, _label in topo.ZONES:
            assert counts[zone_id] >= 1, f"zone {zone_id} has no nodes"

    def test_all_edges_reference_real_nodes(self) -> None:
        node_ids = {n.id for n in topo.NODES}
        for src, dst in topo.EDGES:
            assert src in node_ids, f"edge src '{src}' not in NODES"
            assert dst in node_ids, f"edge dst '{dst}' not in NODES"

    def test_llm_nodes_carry_opus_badge(self) -> None:
        # Every node that should call Claude is tagged Opus 4.7.
        llm_ids = {"risk_debate", "strategy_architect", "unblock_debate",
                   "decision_reflector", "promotion_debate"}
        for nid in llm_ids:
            n = topo.node_by_id(nid)
            assert n is not None, nid
            assert n.model_badge == "Opus 4.7", f"{nid} missing Opus 4.7 badge"

    def test_mailbox_routed_nodes_marked(self) -> None:
        mailbox_nodes = {n.id for n in topo.NODES if n.mailbox}
        # Per the plan: unblock_debate (default ON) and decision_reflector (opt-in).
        assert "unblock_debate" in mailbox_nodes
        assert "decision_reflector" in mailbox_nodes
