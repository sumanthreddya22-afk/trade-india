"""Phase 2 — policy loader: hash verify + load + cooldown."""
from __future__ import annotations

import datetime as dt
import hashlib
import shutil
from pathlib import Path

import pytest

from trading_bot.risk import (
    DEFAULT_POLICY_DIR, PolicyHashMismatch, honor_cooldown, load_policy,
    verify_policy_hashes,
)
from trading_bot.risk.policy_loader import parse_lock_version_date


def test_repo_policy_passes_hash_check() -> None:
    """The shipped policy/HASHES must agree with the on-disk locks."""
    manifest = verify_policy_hashes()
    assert "policy/risk_policy.lock" in manifest
    assert "policy/pdt_policy.lock" in manifest


def test_load_policy_bundle_has_all_keys() -> None:
    bundle = load_policy()
    for attr in (
        "validation_policy", "risk_policy", "pdt_policy", "lane_caps",
        "cost_model", "role_personas", "source_reliability",
        "data_freshness", "short_policy",
    ):
        assert getattr(bundle, attr), f"{attr} empty"
    assert len(bundle.combined_hash) == 64


def test_hash_mismatch_after_in_place_edit(tmp_path: Path) -> None:
    """Copy the policy dir, alter a lock without regenerating HASHES,
    confirm load_policy refuses to load."""
    dst = tmp_path / "policy"
    shutil.copytree(DEFAULT_POLICY_DIR, dst)
    target = dst / "risk_policy.lock"
    target.write_text(target.read_text() + "\n  ")
    with pytest.raises(PolicyHashMismatch):
        load_policy(policy_dir=dst, verify=True)


def test_load_with_verify_false_skips_hash_check(tmp_path: Path) -> None:
    dst = tmp_path / "policy"
    shutil.copytree(DEFAULT_POLICY_DIR, dst)
    (dst / "risk_policy.lock").write_text(
        (dst / "risk_policy.lock").read_text() + "\n"
    )
    bundle = load_policy(policy_dir=dst, verify=False)
    assert bundle.risk_policy


def test_parse_lock_version_date_happy_path() -> None:
    d = parse_lock_version_date("2026-05-13.v4-phase2")
    assert d == dt.date(2026, 5, 13)


def test_parse_lock_version_date_bad_returns_none() -> None:
    assert parse_lock_version_date("not-a-date") is None
    assert parse_lock_version_date("") is None


def test_honor_cooldown_tightening_is_immediate() -> None:
    assert honor_cooldown(
        new_lock_version="2026-05-13.v4",
        new_is_looser=False, today=dt.date(2026, 5, 13),
    ) is True


def test_honor_cooldown_loosening_waits_7_days() -> None:
    today = dt.date(2026, 5, 13)
    # Lock dated today, loosening → not honored yet
    assert honor_cooldown(
        new_lock_version="2026-05-13.v4",
        new_is_looser=True, today=today,
    ) is False
    # Lock dated 6 days ago, loosening → still not honored
    assert honor_cooldown(
        new_lock_version="2026-05-07.v4",
        new_is_looser=True, today=today,
    ) is False
    # Lock dated 7+ days ago, loosening → honored
    assert honor_cooldown(
        new_lock_version="2026-05-06.v4",
        new_is_looser=True, today=today,
    ) is True
