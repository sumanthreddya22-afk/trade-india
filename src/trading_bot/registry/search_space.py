"""Search-space loader.

Plan v4 §8: "Search space declared in research/search_space_v1.json
(hash-locked). The mutation_id and its strategy variant are
deterministically generated from the registered search space; the LLM
only selects which variants to prioritise."

Phase 4 ships the loader + dimension validator. Phase 6 mutation engine
consumes this.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = REPO_ROOT / "research" / "search_space_v1.json"


class SearchSpaceError(Exception):
    """Raised for malformed search-space files or invalid mutation_ids."""


@dataclass(frozen=True)
class SearchSpace:
    """In-memory view of a search-space file."""

    schema_version: int
    thesis_id: str
    dimensions: Mapping[str, Mapping[str, Any]]
    mutation_budget: Mapping[str, Any]
    exclusions: Mapping[str, Any]
    content_hash: str
    source_path: Path


def load_search_space(path: Path = DEFAULT_PATH) -> SearchSpace:
    if not path.exists():
        raise SearchSpaceError(f"search space file not found: {path}")
    text = path.read_text()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise SearchSpaceError(f"invalid JSON in {path}: {e}") from e

    required = ("schema_version", "thesis_id", "dimensions", "mutation_budget")
    missing = [k for k in required if k not in payload]
    if missing:
        raise SearchSpaceError(f"missing required keys: {missing}")
    if int(payload["schema_version"]) != 1:
        raise SearchSpaceError(
            f"unsupported schema_version: {payload['schema_version']}"
        )
    return SearchSpace(
        schema_version=int(payload["schema_version"]),
        thesis_id=payload["thesis_id"],
        dimensions=payload["dimensions"],
        mutation_budget=payload["mutation_budget"],
        exclusions=payload.get("exclusions", {}),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source_path=path,
    )


def validate_mutation_id(
    mutation_id: str, space: SearchSpace,
) -> bool:
    """Return True iff ``mutation_id`` maps to a dimension in the space.

    Plan §8: "mutation_id must map to a dimension in the registered
    search space; ad-hoc additions are rejected at intake."
    """
    return mutation_id in space.dimensions


def list_dimensions(space: SearchSpace) -> list[str]:
    return list(space.dimensions.keys())


def get_dimension(space: SearchSpace, mutation_id: str) -> Mapping[str, Any]:
    if not validate_mutation_id(mutation_id, space):
        raise SearchSpaceError(
            f"mutation_id {mutation_id!r} not in registered search space "
            f"(known: {list_dimensions(space)})"
        )
    return space.dimensions[mutation_id]


__all__ = [
    "DEFAULT_PATH",
    "SearchSpace",
    "SearchSpaceError",
    "get_dimension",
    "list_dimensions",
    "load_search_space",
    "validate_mutation_id",
]
