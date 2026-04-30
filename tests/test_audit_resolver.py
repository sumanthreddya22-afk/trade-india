"""W1.3 — Audit object resolver.

`trading_bot.audit` is a deterministic, no-side-effects utility that computes
the per-decision AuditObject (policy_version, strategy_version, model_versions,
prompt_versions, data_snapshot_ids, regime, risk_state_id, timestamp_utc).

It must: produce stable hashes for stable inputs; be cheap (no DB queries on
the hot path); accept caller-supplied snapshots so the orchestrator stays in
control of what gets recorded.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_bot.audit import (
    build_audit,
    compute_policy_version,
    hash_string,
)
from trading_bot.orchestrator import AuditObject


class TestHashString:
    def test_stable_for_same_input(self):
        assert hash_string("hello") == hash_string("hello")

    def test_changes_for_different_input(self):
        assert hash_string("hello") != hash_string("world")

    def test_short_hex(self):
        h = hash_string("anything")
        assert len(h) == 16
        int(h, 16)  # parses as hex


class TestPolicyVersion:
    def test_with_git_sha_and_config_file(self, tmp_path: Path):
        config = tmp_path / "paper_active.json"
        config.write_text(json.dumps({"version": "v3", "params": {"rsi": 55}}))
        v = compute_policy_version(config_path=config, git_sha="abc1234567")
        # Format: <git_sha[:8]>_<config_hash[:8]>
        assert v.startswith("abc12345_")
        assert len(v.split("_")) == 2
        # Stable for stable inputs
        v2 = compute_policy_version(config_path=config, git_sha="abc1234567")
        assert v == v2

    def test_changes_when_config_changes(self, tmp_path: Path):
        config = tmp_path / "paper_active.json"
        config.write_text(json.dumps({"version": "v3"}))
        v_a = compute_policy_version(config_path=config, git_sha="abc1234567")
        config.write_text(json.dumps({"version": "v4"}))
        v_b = compute_policy_version(config_path=config, git_sha="abc1234567")
        assert v_a != v_b

    def test_changes_when_git_sha_changes(self, tmp_path: Path):
        config = tmp_path / "paper_active.json"
        config.write_text(json.dumps({"version": "v3"}))
        v_a = compute_policy_version(config_path=config, git_sha="aaaa")
        v_b = compute_policy_version(config_path=config, git_sha="bbbb")
        assert v_a != v_b

    def test_missing_config_uses_empty_hash_no_crash(self, tmp_path: Path):
        config = tmp_path / "missing.json"
        v = compute_policy_version(config_path=config, git_sha="abc1234567")
        assert v.startswith("abc12345_")
        assert "_" in v


class TestBuildAudit:
    def test_minimal_inputs_produces_audit_object(self):
        a = build_audit(
            strategy="momentum",
            regime="trending_up",
            policy_version="abc_def",
        )
        assert isinstance(a, AuditObject)
        assert a.strategy_version.startswith("momentum:")
        assert a.policy_version == "abc_def"
        assert a.regime == "trending_up"
        # timestamp_utc auto-populated, ISO 8601 zulu
        assert a.timestamp_utc.endswith("Z")
        assert "T" in a.timestamp_utc

    def test_with_explicit_timestamp(self):
        a = build_audit(
            strategy="momentum",
            regime="trending_up",
            policy_version="x",
            timestamp_utc="2026-04-29T19:41:00Z",
        )
        assert a.timestamp_utc == "2026-04-29T19:41:00Z"

    def test_with_model_and_prompt_versions(self):
        a = build_audit(
            strategy="news_trader",
            regime="trending_up",
            policy_version="x",
            model_versions={"news_controller": "claude-opus-4-7"},
            prompt_versions={"news_controller": "v1:abc"},
        )
        assert a.model_versions == {"news_controller": "claude-opus-4-7"}
        assert a.prompt_versions == {"news_controller": "v1:abc"}

    def test_with_data_snapshots(self):
        a = build_audit(
            strategy="momentum",
            regime="trending_up",
            policy_version="x",
            data_snapshot_ids=("alpaca_bars:NVDA:2026-04-29T19:00", "fred:VIX:2026-04-29"),
        )
        assert "alpaca_bars:NVDA:2026-04-29T19:00" in a.data_snapshot_ids
        assert len(a.data_snapshot_ids) == 2

    def test_strategy_version_includes_strategy_hash(self):
        a = build_audit(strategy="momentum", regime="r", policy_version="x")
        b = build_audit(strategy="mean_reversion", regime="r", policy_version="x")
        assert a.strategy_version != b.strategy_version
        # Both contain a colon-separated hash suffix
        assert ":" in a.strategy_version
        assert ":" in b.strategy_version
