"""Benjamini-Hochberg false-discovery-rate correction.

Plan v4 §8: "BH-FDR applied across the month's candidate set". The
procedure controls the expected proportion of false positives among
rejections (rather than the family-wise error rate).

For m candidates with raw p-values p₁ ≤ p₂ ≤ … ≤ pₘ, find the largest
k such that pₖ ≤ k/m · α; reject hypotheses 1..k. The adjusted p-value
for candidate (i) is ``min_{j ≥ i}(p_j · m / j)`` clamped to ≤ 1.

Phase 6 implementation reads ``mutation_outcome`` rows for a cycle,
applies BH-FDR, and writes back the adjusted_p / survived flags via a
sibling event row (the ``UPDATE``-forbidden trigger makes us append
rather than mutate).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash
from trading_bot.research.mutation_schema import ensure_mutation_tables


@dataclass(frozen=True)
class BHFDRRow:
    candidate_id: str
    raw_p_value: float
    adjusted_p_value: float
    survived: bool


@dataclass(frozen=True)
class BHFDRReport:
    cycle_id: str
    alpha: float
    n_candidates: int
    n_survivors: int
    rows: tuple[BHFDRRow, ...]


def adjust(p_values: Sequence[float]) -> list[float]:
    """Compute BH-adjusted p-values in INPUT order.

    Returns the adjusted p-values aligned positionally with the input.
    """
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda kv: kv[1])
    adjusted_sorted: list[float] = [0.0] * m
    running_min = 1.0
    # Walk from largest to smallest p; track running min of p_j * m / j.
    for k in range(m, 0, -1):
        _orig_idx, p = indexed[k - 1]
        adj = p * m / k
        running_min = min(running_min, adj)
        adjusted_sorted[k - 1] = max(0.0, min(1.0, running_min))
    out = [0.0] * m
    for sorted_pos, (orig_idx, _p) in enumerate(indexed):
        out[orig_idx] = adjusted_sorted[sorted_pos]
    return out


def apply(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    alpha: float = 0.10,
    now: dt.datetime | None = None,
) -> BHFDRReport:
    """Apply BH-FDR across all candidates in a cycle.

    Reads the latest mutation_outcome row per candidate (one outcome per
    candidate is the contract; the loop tolerates multiple but uses the
    most recent). Writes a new mutation_outcome row per candidate with
    the adjusted_p_value + survived populated.
    """
    ensure_mutation_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ml.candidate_id, mo.raw_p_value
        FROM mutation_log ml
        JOIN mutation_outcome mo
          ON mo.candidate_id = ml.candidate_id
          AND mo.ledger_seq = (
              SELECT MAX(ledger_seq) FROM mutation_outcome
              WHERE candidate_id = ml.candidate_id
                AND adjusted_p_value IS NULL
          )
        WHERE ml.cycle_id = ?
        """,
        (cycle_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return BHFDRReport(cycle_id=cycle_id, alpha=alpha,
                           n_candidates=0, n_survivors=0, rows=())

    ids = [r[0] for r in rows]
    raw = [float(r[1]) for r in rows]
    adjusted = adjust(raw)

    out_rows: list[BHFDRRow] = []
    for cid, p, adj in zip(ids, raw, adjusted):
        survived = adj <= alpha
        out_rows.append(BHFDRRow(
            candidate_id=cid, raw_p_value=p,
            adjusted_p_value=adj, survived=survived,
        ))
        prev = last_hash(conn, "mutation_outcome")
        row_for_hash = {
            "candidate_id": cid,
            "outcome_ts": now.isoformat(),
            "raw_p_value": p,
            "adjusted_p_value": adj,
            "survived": 1 if survived else 0,
            "sanity_checks": "{}",
        }
        this_hash = compute_this_hash(prev, row_for_hash)
        conn.execute(
            """
            INSERT INTO mutation_outcome (
                candidate_id, outcome_ts, raw_p_value, adjusted_p_value,
                survived, sanity_checks, prev_hash, this_hash
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                cid, row_for_hash["outcome_ts"], p, adj,
                1 if survived else 0, "{}", prev, this_hash,
            ),
        )

    return BHFDRReport(
        cycle_id=cycle_id, alpha=alpha,
        n_candidates=len(out_rows),
        n_survivors=sum(1 for r in out_rows if r.survived),
        rows=tuple(out_rows),
    )


__all__ = ["BHFDRReport", "BHFDRRow", "adjust", "apply"]
