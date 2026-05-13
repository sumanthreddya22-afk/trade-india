"""P0 acceptance: policy/HASHES is well-formed and up-to-date.

Plan v4 §14: "Three policy lock files + HASHES — Mutating any lock file
without updating HASHES halts startup with explicit error."

Phase 0 ships nine skeleton locks + eight persona files + the edge
thesis, all hashed into ``policy/HASHES``. The runtime startup check
lands in Phase 2; for now we verify the manifest itself is correct.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HASHES = REPO_ROOT / "policy" / "HASHES"


def _parse_hashes() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in HASHES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, rel = line.split(None, 1)
        out[rel] = digest
    return out


def test_hashes_file_exists() -> None:
    assert HASHES.exists(), "policy/HASHES must exist"


def test_hashes_lists_all_nine_locks() -> None:
    parsed = _parse_hashes()
    expected = {
        "policy/validation_policy.lock",
        "policy/risk_policy.lock",
        "policy/pdt_policy.lock",
        "policy/lane_caps.lock",
        "policy/cost_model.lock",
        "policy/role_personas.lock",
        "policy/source_reliability.lock",
        "policy/data_freshness.lock",
        "policy/short_policy.lock",
    }
    missing = expected - parsed.keys()
    assert not missing, f"HASHES missing locks: {sorted(missing)}"


def test_hashes_lists_all_eight_personas() -> None:
    parsed = _parse_hashes()
    expected = {
        "prompts/roles/quant_pm.v1.md",
        "prompts/roles/quant_research_lead.v1.md",
        "prompts/roles/risk_validator.v1.md",
        "prompts/roles/trading_systems_engineer.v1.md",
        "prompts/roles/execution_engineer.v1.md",
        "prompts/roles/ai_mlops.v1.md",
        "prompts/roles/sre_ops.v1.md",
        "prompts/roles/compliance.v1.md",
    }
    missing = expected - parsed.keys()
    assert not missing, f"HASHES missing personas: {sorted(missing)}"


def test_hashes_lists_edge_thesis() -> None:
    parsed = _parse_hashes()
    assert "docs/edge_thesis_v1.md" in parsed


def test_each_listed_file_matches_its_hash() -> None:
    parsed = _parse_hashes()
    for rel, expected in parsed.items():
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} listed in HASHES but missing on disk"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, (
            f"hash mismatch for {rel}: expected {expected}, got {actual}"
        )


def test_recompute_hashes_check_passes() -> None:
    """The tool's --check mode must agree with what's on disk right now."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "recompute_hashes.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"recompute_hashes.py --check failed: {result.stdout} {result.stderr}"
    )
