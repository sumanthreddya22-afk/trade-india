"""Phase 6 — Benjamini-Hochberg FDR adjust + apply."""
from __future__ import annotations

from trading_bot.ledger import verify_chain
from trading_bot.registry import load_search_space
from trading_bot.research import (
    bh_fdr_adjust, bh_fdr_apply,
    propose_candidates, record_candidate, record_outcome,
)


def test_adjust_empty() -> None:
    assert bh_fdr_adjust([]) == []


def test_adjust_classic_example() -> None:
    # Classic textbook check: with p = [0.01, 0.04, 0.06, 0.20], adjusted:
    #   p_(4)*4/4 = 0.20
    #   p_(3)*4/3 = 0.08   → running_min(0.20) = 0.08
    #   p_(2)*4/2 = 0.08   → running_min(0.08) = 0.08
    #   p_(1)*4/1 = 0.04   → running_min(0.08) = 0.04
    adj = bh_fdr_adjust([0.01, 0.04, 0.06, 0.20])
    assert abs(adj[0] - 0.04) < 1e-9
    assert abs(adj[1] - 0.08) < 1e-9
    assert abs(adj[2] - 0.08) < 1e-9
    assert abs(adj[3] - 0.20) < 1e-9


def test_adjust_clamps_at_one() -> None:
    adj = bh_fdr_adjust([0.9, 0.95])
    for v in adj:
        assert 0 <= v <= 1


def test_adjust_preserves_input_order() -> None:
    raw = [0.5, 0.1, 0.3]
    adj = bh_fdr_adjust(raw)
    # Manually: sorted = [0.1, 0.3, 0.5]; adj_sorted = [0.3, 0.45, 0.5]
    # → un-sorted positions: [0.5 -> 0.5, 0.1 -> 0.3, 0.3 -> 0.45]
    assert abs(adj[0] - 0.5) < 1e-9
    assert abs(adj[1] - 0.3) < 1e-9
    assert abs(adj[2] - 0.45) < 1e-9


def _seed_cycle(conn, *, cycle="2026-05", values=(0.01, 0.04, 0.06, 0.20)):
    """Insert 4 mutation_log rows with distinct candidate_ids using
    different mutation values."""
    space = load_search_space()
    cands = propose_candidates(
        thesis_id="t", cycle_id=cycle, search_space=space,
        mutation_ids=["parameter:lookback_months"],
    )
    inserted = []
    for c, p in zip(cands[:len(values)], values):
        record_candidate(conn, c)
        record_outcome(conn, candidate_id=c.candidate_id, raw_p_value=p)
        inserted.append((c.candidate_id, p))
    return inserted


def test_apply_writes_adjusted_rows(ledger_conn) -> None:
    _seed_cycle(ledger_conn)
    rep = bh_fdr_apply(ledger_conn, cycle_id="2026-05", alpha=0.10)
    assert rep.n_candidates == 4
    # The classic example: with α=0.10 only the smallest p (0.01) survives;
    # adjusted p=0.04 ≤ 0.10. The others are 0.08, 0.08, 0.20 — only 0.08
    # values ALSO survive! Let me double-check: 0.08 ≤ 0.10, so yes
    # candidates at positions 1, 2 (adjusted 0.08) survive too.
    assert rep.n_survivors == 3
    # And the chain is intact after writes.
    assert verify_chain(ledger_conn, "mutation_outcome") > 0


def test_apply_empty_cycle_is_noop(ledger_conn) -> None:
    rep = bh_fdr_apply(ledger_conn, cycle_id="2026-05", alpha=0.10)
    assert rep.n_candidates == 0
    assert rep.n_survivors == 0
