"""Phase 4 — search space loader + dimension validator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_bot.registry import (
    SearchSpaceError, get_dimension, list_dimensions, load_search_space,
    validate_mutation_id,
)
from trading_bot.registry.search_space import DEFAULT_PATH


def test_load_shipped_search_space() -> None:
    space = load_search_space()
    assert space.thesis_id == "edge_thesis_v1"
    assert space.schema_version == 1
    assert len(space.content_hash) == 64


def test_dimensions_include_etf_momentum_v1_set() -> None:
    space = load_search_space()
    dims = list_dimensions(space)
    assert "parameter:lookback_months" in dims
    assert "universe:etf_set" in dims


def test_validate_mutation_id_known() -> None:
    space = load_search_space()
    assert validate_mutation_id("parameter:lookback_months", space)
    assert not validate_mutation_id("parameter:ghost", space)


def test_get_dimension_rejects_unknown() -> None:
    space = load_search_space()
    with pytest.raises(SearchSpaceError):
        get_dimension(space, "parameter:ghost")


def test_load_search_space_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SearchSpaceError):
        load_search_space(tmp_path / "missing.json")


def test_load_search_space_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json")
    with pytest.raises(SearchSpaceError):
        load_search_space(p)


def test_load_search_space_missing_required_keys(tmp_path: Path) -> None:
    p = tmp_path / "incomplete.json"
    p.write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(SearchSpaceError, match=r"missing required keys"):
        load_search_space(p)


def test_search_space_is_in_hashes_manifest() -> None:
    """v4 §8: search space is hash-locked. Confirm policy/HASHES tracks it."""
    repo_root = Path(__file__).resolve().parent.parent
    hashes_path = repo_root / "policy" / "HASHES"
    body = hashes_path.read_text()
    assert "research/search_space_v1.json" in body
