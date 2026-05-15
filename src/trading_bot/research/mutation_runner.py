"""Wire ``run_mutation_cycle`` to the daemon (v4 Phase C).

Provides:

  * ``run_nightly_cycle``    — nightly job entry point.
  * ``run_weekly_review``    — Sunday mutation_reviewer memo.
  * ``run_monthly_expansion``— monthly search_space_expander memo.

These are intentionally small wrappers — heavy lifting lives in
``mutation_engine`` / ``paper_validation`` / ``shared.llm_transport``.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from trading_bot.ledger import connect_writer

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


def run_nightly_cycle(
    ledger_db: Path,
    *,
    policy_dir: Path,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Run one mutation cycle per registered v3 family.

    For each family:
      1. Propose candidates from search_space_v1.json (capped at the
         budget in mutation_engine).
      2. Backtest each candidate (callable resolved from the family
         module if available; else dry-run "passes with raw_p=0.5").
      3. Apply BH-FDR.
      4. For survivors, run paper-submit validation.
      5. On pass, mark the candidate for auto-registration.

    The full pipeline assumes an operator has wired a backtest
    harness; the scaffold gracefully degrades when that is missing.
    """
    out: dict = {"families_run": [], "n_candidates": 0, "n_passed": 0}
    try:
        from trading_bot.research.run_mutation_cycle import run_cycle
        from trading_bot.registry.search_space import load_search_space
    except ImportError as e:
        return {"error": f"import: {e}"}

    families = [
        "DUAL_MOMENTUM_v3",
        "ETF_MOMENTUM_v3",
        "CRYPTO_MOMENTUM_v3",
        # SPY_WHEEL_v3 is skipped: its parameter mutations need an
        # option-chain backtest harness which isn't shipped yet. The
        # mutation engine framework supports it; the family entry in
        # mutation_backtest._signal_registry would need a wheel-aware
        # backtest fn.
    ]
    conn = connect_writer(ledger_db)
    try:
        try:
            from trading_bot.research.mutation_backtest import make_backtest_fn
            _backtest = make_backtest_fn()
        except Exception as e:  # noqa: BLE001
            log.warning("real backtest unavailable; falling back to dryrun: %s", e)

            def _backtest(candidate):  # noqa: ANN001
                return 0.5, {"dryrun": True, "fallback_reason": str(e)}

        for family in families:
            try:
                space = load_search_space()
            except Exception as e:  # noqa: BLE001
                log.warning("search_space load failed: %s", e)
                continue

            try:
                report = run_cycle(
                    conn,
                    thesis_id=family.lower(),
                    cycle_id=f"{family}-{(now or dt.datetime.utcnow()).strftime('%Y%m%d')}",
                    search_space=space,
                    backtest=_backtest,
                )
                out["families_run"].append(family)
                out["n_candidates"] += len(report.candidates) if hasattr(report, "candidates") else 0
            except Exception as e:  # noqa: BLE001
                log.warning("mutation cycle %s failed: %s", family, e)
        conn.commit()
    finally:
        conn.close()
    return out


def run_weekly_review(ledger_db: Path) -> dict:
    """Emit a ``mutation_review_event`` row via the mutation_reviewer
    persona for the last 7 days of mutation_outcome rows.

    Skips gracefully when the LLM is unavailable.
    """
    try:
        from trading_bot.ledger.mutation_review_event import write_event
        from trading_bot.shared.llm_transport import LLMUnavailable, invoke
        from trading_bot.research.persona_runner import verify_persona_hash
    except ImportError as e:
        return {"error": f"import: {e}"}

    persona_path = REPO_ROOT / "prompts" / "roles" / "mutation_reviewer.v1.md"
    hashes_path = REPO_ROOT / "policy" / "HASHES"
    try:
        persona_hash = verify_persona_hash(persona_path, hashes_path=hashes_path)
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"persona hash: {e}"}

    conn = connect_writer(ledger_db)
    try:
        # Pull last 7 days of mutation_outcome (if table exists).
        try:
            cur = conn.execute(
                "SELECT * FROM mutation_outcome "
                "WHERE event_ts >= datetime('now', '-7 days') "
                "ORDER BY ledger_seq DESC LIMIT 200"
            )
            rows = [dict(zip([c[0] for c in cur.description], r))
                    for r in cur.fetchall()]
        except sqlite3.OperationalError:
            rows = []
        prompt = (
            f"# Persona (sha256:{persona_hash})\n\n"
            + persona_path.read_text()
            + f"\n\n# Mutation outcomes (last 7 days, n={len(rows)})\n"
            + json.dumps(rows[:50], default=str, indent=2)
        )
        try:
            resp = invoke(role="mutation_reviewer", prompt=prompt, conn=conn)
        except LLMUnavailable as e:
            return {"skipped": str(e)}
        memo = resp.text
        n_passed = 0
        try:
            parsed = json.loads(resp.text)
            if isinstance(parsed, dict):
                memo = parsed.get("memo_markdown", resp.text)
                n_passed = len(parsed.get("winners", []))
        except json.JSONDecodeError:
            pass
        seq = write_event(
            conn,
            review_window_iso=dt.datetime.utcnow().strftime("%Y-W%V"),
            persona_id="mutation_reviewer",
            persona_hash=f"sha256:{persona_hash}",
            n_candidates=len(rows),
            n_passed=n_passed,
            memo_markdown=memo,
        )
        conn.commit()
        return {"event_seq": seq, "n_candidates": len(rows), "n_passed": n_passed}
    finally:
        conn.close()


def run_monthly_expansion(ledger_db: Path) -> dict:
    """Emit a ``search_space_proposal_event`` via the persona for the
    last month of mutation_outcome rows."""
    try:
        from trading_bot.ledger.search_space_proposal_event import write_event
        from trading_bot.shared.llm_transport import LLMUnavailable, invoke
        from trading_bot.research.persona_runner import verify_persona_hash
    except ImportError as e:
        return {"error": f"import: {e}"}

    persona_path = REPO_ROOT / "prompts" / "roles" / "search_space_expander.v1.md"
    hashes_path = REPO_ROOT / "policy" / "HASHES"
    search_space_path = REPO_ROOT / "research" / "search_space_v1.json"
    try:
        persona_hash = verify_persona_hash(persona_path, hashes_path=hashes_path)
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"persona hash: {e}"}

    try:
        current_body = search_space_path.read_text()
        import hashlib
        current_hash = hashlib.sha256(current_body.encode()).hexdigest()
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"search_space read: {e}"}

    conn = connect_writer(ledger_db)
    try:
        try:
            cur = conn.execute(
                "SELECT * FROM mutation_outcome "
                "WHERE event_ts >= datetime('now', '-31 days') LIMIT 500"
            )
            rows = [dict(zip([c[0] for c in cur.description], r))
                    for r in cur.fetchall()]
        except sqlite3.OperationalError:
            rows = []
        prompt = (
            f"# Persona (sha256:{persona_hash})\n\n"
            + persona_path.read_text()
            + f"\n\n# Current search_space_v1.json\n```\n{current_body}\n```\n"
            + f"\n# Mutation outcomes (last month, n={len(rows)})\n"
            + json.dumps(rows[:100], default=str, indent=2)
        )
        try:
            resp = invoke(role="search_space_expander", prompt=prompt, conn=conn)
        except LLMUnavailable as e:
            return {"skipped": str(e)}
        memo = resp.text
        proposed: dict = {}
        try:
            parsed = json.loads(resp.text)
            if isinstance(parsed, dict):
                memo = parsed.get("memo_markdown", resp.text)
                proposed = {
                    "additions": parsed.get("proposed_additions", []),
                    "retire": parsed.get("dimensions_to_retire", []),
                }
        except json.JSONDecodeError:
            pass
        seq = write_event(
            conn,
            review_month_iso=dt.datetime.utcnow().strftime("%Y-%m"),
            persona_id="search_space_expander",
            persona_hash=f"sha256:{persona_hash}",
            current_hash=current_hash,
            proposed_additions=proposed,
            memo_markdown=memo,
        )
        conn.commit()
        return {"event_seq": seq, "n_outcomes_reviewed": len(rows)}
    finally:
        conn.close()


__all__ = [
    "run_monthly_expansion",
    "run_nightly_cycle",
    "run_weekly_review",
]
