"""Hash-chained appends for the v4 Phase D research-bot ledger tables.

Three tables co-located here because they all participate in the
discovery pipeline (scout -> candidate -> blueprint -> codegen).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Mapping, Optional, Sequence

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash


# ----- source_scout_event --------------------------------------------------

def write_source_scout(
    conn: sqlite3.Connection,
    *,
    source: str,
    items_seen: int,
    items_above_quality: int,
    items_deduplicated: int,
    items_candidates_created: int,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "source_scout_event")
    row = {
        "event_ts": now.isoformat(),
        "source": source,
        "items_seen": int(items_seen),
        "items_above_quality": int(items_above_quality),
        "items_deduplicated": int(items_deduplicated),
        "items_candidates_created": int(items_candidates_created),
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO source_scout_event (
            event_ts, source, items_seen, items_above_quality,
            items_deduplicated, items_candidates_created,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["event_ts"], row["source"], row["items_seen"],
         row["items_above_quality"], row["items_deduplicated"],
         row["items_candidates_created"], prev, this_hash),
    )
    return int(cur.lastrowid)


# ----- strategy_candidate --------------------------------------------------

CANDIDATE_STATUSES = ("pending", "approved", "rejected", "implemented")


def write_candidate(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_ref: str,
    raw_content_hash: str,
    title: str,
    summary_md: str,
    taxonomy_tags: Sequence[str],
    quality_score: float,
    status: str = "pending",
    now: Optional[dt.datetime] = None,
) -> int:
    if status not in CANDIDATE_STATUSES:
        raise ValueError(
            f"status must be one of {CANDIDATE_STATUSES}, got {status!r}"
        )
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "strategy_candidate")
    row = {
        "event_ts": now.isoformat(),
        "source": source,
        "source_ref": source_ref,
        "raw_content_hash": raw_content_hash,
        "title": title,
        "summary_md": summary_md,
        "taxonomy_tags_json": json.dumps(
            list(taxonomy_tags), sort_keys=True, separators=(",", ":"),
        ),
        "quality_score": float(quality_score),
        "status": status,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO strategy_candidate (
            event_ts, source, source_ref, raw_content_hash, title,
            summary_md, taxonomy_tags_json, quality_score, status,
            prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["event_ts"], row["source"], row["source_ref"],
         row["raw_content_hash"], row["title"], row["summary_md"],
         row["taxonomy_tags_json"], row["quality_score"], row["status"],
         prev, this_hash),
    )
    return int(cur.lastrowid)


def candidate_exists(conn: sqlite3.Connection, raw_content_hash: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM strategy_candidate WHERE raw_content_hash=? LIMIT 1",
        (raw_content_hash,),
    )
    return cur.fetchone() is not None


# ----- strategy_blueprint --------------------------------------------------

BLUEPRINT_VERDICTS = ("approved", "rejected")


def write_blueprint(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    blueprint_md: str,
    params: Mapping,
    universe_filter: Mapping,
    data_needs: Sequence[str],
    data_available: bool,
    intake_transcript_id: str,
    intake_verdict: str,
    now: Optional[dt.datetime] = None,
) -> int:
    if intake_verdict not in BLUEPRINT_VERDICTS:
        raise ValueError(
            f"intake_verdict must be one of {BLUEPRINT_VERDICTS}, "
            f"got {intake_verdict!r}"
        )
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "strategy_blueprint")
    row = {
        "event_ts": now.isoformat(),
        "candidate_id": int(candidate_id),
        "blueprint_md": blueprint_md,
        "params_json": json.dumps(
            dict(params), sort_keys=True, separators=(",", ":"), default=str,
        ),
        "universe_filter_json": json.dumps(
            dict(universe_filter), sort_keys=True, separators=(",", ":"),
        ),
        "data_needs_json": json.dumps(
            list(data_needs), sort_keys=True, separators=(",", ":"),
        ),
        "data_available": 1 if data_available else 0,
        "intake_transcript_id": intake_transcript_id,
        "intake_verdict": intake_verdict,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO strategy_blueprint (
            event_ts, candidate_id, blueprint_md, params_json,
            universe_filter_json, data_needs_json, data_available,
            intake_transcript_id, intake_verdict, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["event_ts"], row["candidate_id"], row["blueprint_md"],
         row["params_json"], row["universe_filter_json"],
         row["data_needs_json"], row["data_available"],
         row["intake_transcript_id"], row["intake_verdict"],
         prev, this_hash),
    )
    return int(cur.lastrowid)


# ----- strategy_codegen_event ---------------------------------------------

def write_codegen(
    conn: sqlite3.Connection,
    *,
    blueprint_id: int,
    new_family_id: str,
    runner_path: str,
    tests_path: str,
    ruff_pass: bool,
    mypy_pass: bool,
    test_pass: bool,
    registered: bool,
    now: Optional[dt.datetime] = None,
) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = last_hash(conn, "strategy_codegen_event")
    row = {
        "event_ts": now.isoformat(),
        "blueprint_id": int(blueprint_id),
        "new_family_id": new_family_id,
        "runner_path": runner_path,
        "tests_path": tests_path,
        "ruff_pass": 1 if ruff_pass else 0,
        "mypy_pass": 1 if mypy_pass else 0,
        "test_pass": 1 if test_pass else 0,
        "registered": 1 if registered else 0,
    }
    this_hash = compute_this_hash(prev, row)
    cur = conn.execute(
        """
        INSERT INTO strategy_codegen_event (
            event_ts, blueprint_id, new_family_id, runner_path, tests_path,
            ruff_pass, mypy_pass, test_pass, registered, prev_hash, this_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["event_ts"], row["blueprint_id"], row["new_family_id"],
         row["runner_path"], row["tests_path"], row["ruff_pass"],
         row["mypy_pass"], row["test_pass"], row["registered"],
         prev, this_hash),
    )
    return int(cur.lastrowid)


__all__ = [
    "BLUEPRINT_VERDICTS",
    "CANDIDATE_STATUSES",
    "candidate_exists",
    "write_blueprint",
    "write_candidate",
    "write_codegen",
    "write_source_scout",
]
