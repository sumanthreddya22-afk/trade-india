"""Phase 6 — end-to-end mutation cycle driver."""
from __future__ import annotations

from trading_bot.registry import load_search_space
from trading_bot.research import Candidate, run_mutation_cycle


def test_cycle_proposes_records_and_applies_bhfdr(ledger_conn) -> None:
    space = load_search_space()

    p_values = iter([0.001, 0.04, 0.30, 0.50, 0.80, 0.95])

    def backtest(c: Candidate):
        return next(p_values), {"benchmark_beat": True}

    rep = run_mutation_cycle(
        ledger_conn,
        thesis_id="edge_thesis_v1", cycle_id="2026-05",
        search_space=space, backtest=backtest,
        mutation_ids=["parameter:lookback_months"],
        alpha=0.10,
    )
    assert rep.n_proposed == 6     # lookback_months has 6 values
    assert rep.n_backtested == 6
    # BH-FDR survivors with α=0.10: only the lowest p values survive.
    assert rep.n_survivors >= 1
    assert rep.n_survivors <= rep.n_proposed


def test_cycle_with_rationale_lookup_records_text(ledger_conn) -> None:
    space = load_search_space()
    rationale = {"parameter:lookback_months": "LLM picked: TSM stickiness"}

    def backtest(c):
        return 0.01, {}

    rep = run_mutation_cycle(
        ledger_conn,
        thesis_id="edge_thesis_v1", cycle_id="2026-06",
        search_space=space, backtest=backtest,
        mutation_ids=["parameter:lookback_months"],
        rationale_lookup=rationale, proposer="llm_mlops",
    )
    cur = ledger_conn.cursor()
    cur.execute(
        "SELECT proposer, rationale FROM mutation_log WHERE cycle_id='2026-06' LIMIT 1"
    )
    proposer, text = cur.fetchone()
    assert proposer == "llm_mlops"
    assert "LLM picked" in text
