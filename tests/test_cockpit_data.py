"""Cockpit data layer — verifies every builder returns the right shape
even against a fresh empty ledger so the cockpit never crashes.

The mock data.jsx baseline shows through any field we can't compute;
these tests make sure we don't accidentally start raising on an empty
table or a missing intel cache."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trading_bot.operator_ui.cockpit_data import (
    _lane_for_strategy, _short_hash, build_state,
    build_lessons, build_lanes, build_regime, build_strategies,
    build_policy_locks, build_personas,
)


# ---- Unit tests for helpers ----------------------------------------------

def test_short_hash_truncates() -> None:
    assert _short_hash("abcdef1234567890") == "abcd…90"
    assert _short_hash("sha256:abcdef1234567890") == "abcd…90"
    assert _short_hash("") == "—"
    assert _short_hash(None) == "—"
    assert _short_hash("abc") == "abc"


def test_lane_for_strategy() -> None:
    assert _lane_for_strategy("ETF_MOMENTUM_v3") == "stocks"
    assert _lane_for_strategy("CRYPTO_MOMENTUM_v3") == "crypto"
    assert _lane_for_strategy("SPY_WHEEL_v3") == "options"
    assert _lane_for_strategy("DUAL_MOMENTUM_v1") == "stocks"


# ---- build_state against empty ledger ------------------------------------

def test_build_state_empty_ledger(tmp_path: Path) -> None:
    """Even with zero rows, every top-level key is present + serialisable."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    from trading_bot.ledger.schema import create_ledger
    create_ledger(conn)
    conn.close()

    state = build_state(ledger_db=db)
    # Every expected global key is present (None is acceptable for
    # builders that wholesale failed; we assert the *expected* keys exist).
    expected = {
        "STATUS_BASE", "LANES", "REGIME", "RISK_CAPS", "STRATEGY_MODE",
        "POSITIONS", "OPEN_ORDERS", "SEED_ACTIVITY", "EXPOSURE_BREAKDOWN",
        "DAILY_DIGEST", "DECISIONS", "LESSONS", "STRATEGIES", "MUTATIONS",
        "PROMOTION_QUEUE", "LLM_SPEND", "JOBS", "FRESHNESS", "POLICY_LOCKS",
        "PERSONAS", "HALTS", "LEDGER_HEALTH", "DAEMON", "COST_MODEL",
        "DRIFT", "RECON",
    }
    assert expected.issubset(state.keys())

    # And the whole payload must JSON-serialise (cockpit overlay needs it).
    json.dumps(state, default=str)


def test_build_state_strategy_versions(tmp_path: Path) -> None:
    """A populated strategy_version row appears in STRATEGIES + STRATEGY_MODE."""
    db = tmp_path / "populated.db"
    conn = sqlite3.connect(str(db))
    from trading_bot.ledger.schema import create_ledger
    from trading_bot.registry.strategies import register_version
    create_ledger(conn)
    register_version(
        conn,
        strategy_id="ETF_MOMENTUM_v3", strategy_ver=1,
        code_hash="abc123def456", config_hash="cfg",
        thesis_id="t", hypothesis_id="h",
        lane="etf_momentum", owner="op",
    )
    conn.commit()
    conn.close()

    state = build_state(ledger_db=db)
    assert any(
        s["name"].startswith("ETF_MOMENTUM_v3")
        for s in state["STRATEGIES"]
    )
    assert any(
        s["name"].startswith("ETF_MOMENTUM_v3")
        for s in state["STRATEGY_MODE"]
    )
    # Lane mapping
    etf = next(
        s for s in state["STRATEGIES"]
        if s["name"].startswith("ETF_MOMENTUM_v3")
    )
    assert etf["lane"] == "stocks"


def test_build_regime_empty_returns_normal(tmp_path: Path) -> None:
    db = tmp_path / "regime.db"
    conn = sqlite3.connect(str(db))
    from trading_bot.ledger.schema import create_ledger
    create_ledger(conn)
    r = build_regime(conn)
    conn.close()
    assert r["label"] == "normal"
    assert len(r["asset_classes"]) == 3
    assert all(ac["regime"] == "normal" for ac in r["asset_classes"])


def test_build_lessons_pulls_postmortem_memos(tmp_path: Path) -> None:
    db = tmp_path / "memos.db"
    conn = sqlite3.connect(str(db))
    from trading_bot.ledger.schema import create_ledger
    from trading_bot.ledger.drift_postmortem_event import write_event
    create_ledger(conn)
    write_event(
        conn, source_event_type="universe_audit_event",
        source_ledger_seq=1, persona_id="universe_audit_analyst",
        persona_hash="sha256:" + "a" * 64,
        memo_markdown="## Test memo\n\nbody body body",
    )
    conn.commit()
    lessons = build_lessons(conn)
    conn.close()
    assert len(lessons) == 1
    assert lessons[0]["tag"] == "universe"
    assert "Test memo" in lessons[0]["body"]


def test_build_policy_locks_includes_phase_a() -> None:
    locks = build_policy_locks()
    names = {l["name"] for l in locks}
    # Phase A locks should all show up.
    assert "paper_fast_track_v1" in names
    assert "etf_universe_v1" in names
    assert "regime_protocols_v1" in names


def test_build_personas_includes_phase_a() -> None:
    personas = build_personas()
    names = {p["name"] for p in personas}
    assert "drift_postmortem.v1" in names
    assert "regime_analyst.v1" in names
    assert "universe_audit_analyst.v1" in names
    # All must have a non-empty hash.
    assert all(p["hash"] != "—" for p in personas)


def test_lanes_caps_from_policy(tmp_path: Path) -> None:
    db = tmp_path / "lanes.db"
    conn = sqlite3.connect(str(db))
    from trading_bot.ledger.schema import create_ledger
    create_ledger(conn)
    lanes = build_lanes(conn)
    conn.close()
    # Three canonical lanes with sensible caps from risk_policy.lock.
    assert len(lanes) == 3
    assert {l["key"] for l in lanes} == {"stocks", "crypto", "options"}
    crypto = next(l for l in lanes if l["key"] == "crypto")
    # crypto_gross_max_pct is 15% per the live policy.
    assert 0.10 <= crypto["cap_pct"] <= 0.20
