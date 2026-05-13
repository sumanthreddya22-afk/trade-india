"""Mutation candidate generator + recorder.

Plan v4 §8: "The mutation_id and its strategy variant are
deterministically generated from the registered search space; the LLM
only selects which variants to prioritise." LLM cannot add dimensions
— search_space_v1.json is hash-locked.

Phase 6 ships:

- ``propose_candidates`` — enumerates valid variants from a SearchSpace
  within the per-family budget; rejects mutation_ids not in the space
  (Plan §14 P1 "LLM hallucinated proposal").
- ``record_candidate`` — appends one row to ``mutation_log``.
- ``record_outcome`` — appends one row to ``mutation_outcome`` (called
  after the backtest produces a raw p-value or t-stat).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash
from trading_bot.registry.search_space import (
    SearchSpace, SearchSpaceError, validate_mutation_id,
)
from trading_bot.research.mutation_schema import ensure_mutation_tables


DEFAULT_BUDGET_PER_FAMILY = 64
"""Plan §8 monthly experiment budget cap."""


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    thesis_id: str
    family: str
    mutation_id: str
    variant_value: str
    cycle_id: str
    hypothesis_hash: str
    rationale: str
    proposer: str


def _candidate_id(thesis_id: str, mutation_id: str, variant_value: str) -> str:
    payload = json.dumps({
        "thesis_id": thesis_id,
        "mutation_id": mutation_id,
        "variant_value": variant_value,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _family_of(mutation_id: str) -> str:
    """family = the part before the colon: 'parameter:lookback_months' →
    'parameter'."""
    return mutation_id.split(":", 1)[0]


def propose_candidates(
    *,
    thesis_id: str,
    cycle_id: str,
    search_space: SearchSpace,
    mutation_ids: Optional[Sequence[str]] = None,
    budget_per_family: int = DEFAULT_BUDGET_PER_FAMILY,
    proposer: str = "mutation_engine",
    rationale_lookup: Optional[Mapping[str, str]] = None,
) -> list[Candidate]:
    """Enumerate candidate variants.

    If ``mutation_ids`` is None, enumerate *all* dimensions in the
    search space. The LLM-driven path (Phase 6+ mutation prioritisation)
    passes a filtered list.

    ``rationale_lookup`` maps mutation_id → human-readable text the
    LLM wrote when proposing the dimension. Stored on the row for audit.

    Per Plan §8 LLM authority box: this function does NOT consult an LLM
    — it deterministically expands the search space. The LLM's
    contribution is the prior list of mutation_ids it prioritised + the
    rationale text.
    """
    if mutation_ids is None:
        mutation_ids = list(search_space.dimensions.keys())

    # Plan §14 P1 — refuse unknown mutation_ids at intake.
    for mid in mutation_ids:
        if not validate_mutation_id(mid, search_space):
            raise SearchSpaceError(
                f"mutation_id {mid!r} not in registered search space"
            )

    out: list[Candidate] = []
    family_count: dict[str, int] = {}
    for mid in mutation_ids:
        dim = search_space.dimensions[mid]
        domain = dim.get("domain", [])
        family = _family_of(mid)
        for value in domain:
            if family_count.get(family, 0) >= budget_per_family:
                break
            value_str = json.dumps(value, sort_keys=True,
                                   separators=(",", ":"), default=str)
            cid = _candidate_id(thesis_id, mid, value_str)
            hypothesis_hash = hashlib.sha256(
                f"{thesis_id}|{mid}|{value_str}".encode("utf-8")
            ).hexdigest()
            rationale = (rationale_lookup or {}).get(mid, "")
            out.append(Candidate(
                candidate_id=cid, thesis_id=thesis_id, family=family,
                mutation_id=mid, variant_value=value_str,
                cycle_id=cycle_id, hypothesis_hash=hypothesis_hash,
                rationale=rationale, proposer=proposer,
            ))
            family_count[family] = family_count.get(family, 0) + 1
    return out


def record_candidate(
    conn: sqlite3.Connection,
    candidate: Candidate,
    *,
    now: Optional[dt.datetime] = None,
) -> int:
    ensure_mutation_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "mutation_log")
    row = {
        "candidate_id": candidate.candidate_id,
        "thesis_id": candidate.thesis_id,
        "family": candidate.family,
        "mutation_id": candidate.mutation_id,
        "variant_value": candidate.variant_value,
        "cycle_id": candidate.cycle_id,
        "proposed_ts": now.isoformat(),
        "hypothesis_hash": candidate.hypothesis_hash,
        "rationale": candidate.rationale,
        "proposer": candidate.proposer,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO mutation_log (
            candidate_id, thesis_id, family, mutation_id, variant_value,
            cycle_id, proposed_ts, hypothesis_hash, rationale, proposer,
            prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["candidate_id"], row["thesis_id"], row["family"],
            row["mutation_id"], row["variant_value"], row["cycle_id"],
            row["proposed_ts"], row["hypothesis_hash"], row["rationale"],
            row["proposer"], prev, this_hash,
        ),
    )
    return cur.lastrowid


def record_outcome(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    raw_p_value: float,
    sanity_checks: Optional[Mapping] = None,
    now: Optional[dt.datetime] = None,
) -> int:
    ensure_mutation_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "mutation_outcome")
    sc_json = json.dumps(dict(sanity_checks or {}),
                         sort_keys=True, separators=(",", ":"),
                         default=str)
    row = {
        "candidate_id": candidate_id,
        "outcome_ts": now.isoformat(),
        "raw_p_value": float(raw_p_value),
        "adjusted_p_value": None,
        "survived": None,
        "sanity_checks": sc_json,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO mutation_outcome (
            candidate_id, outcome_ts, raw_p_value,
            adjusted_p_value, survived, sanity_checks,
            prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            row["candidate_id"], row["outcome_ts"], row["raw_p_value"],
            row["adjusted_p_value"], row["survived"], row["sanity_checks"],
            prev, this_hash,
        ),
    )
    return cur.lastrowid


def list_candidates(
    conn: sqlite3.Connection, *, cycle_id: str,
) -> list[dict]:
    ensure_mutation_tables(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT candidate_id, thesis_id, family, mutation_id, variant_value,
               cycle_id, proposed_ts, hypothesis_hash, rationale, proposer
        FROM mutation_log WHERE cycle_id = ?
        ORDER BY ledger_seq
        """,
        (cycle_id,),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


__all__ = [
    "Candidate",
    "DEFAULT_BUDGET_PER_FAMILY",
    "list_candidates",
    "propose_candidates",
    "record_candidate",
    "record_outcome",
]
