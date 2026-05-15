#!/usr/bin/env python3
"""Phase 12 LLM-transport smoke test.

Run a Sonnet ``hello`` and an Opus ``hello`` through the shared transport,
asserting:

  1. the CLI returns parseable JSON
  2. an ``llm_call_event`` ledger row is appended per call
  3. a second call with the same prompt is a cache hit (no new spend)

Usage::

    python tools/llm_smoke_test.py
    python tools/llm_smoke_test.py --skip-opus    # cheaper run
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_bot.ledger import (  # noqa: E402
    DEFAULT_LEDGER_PATH, connect_writer, ensure_schema,
)
from trading_bot.shared.llm_transport import (  # noqa: E402
    LLMUnavailable, claude_cli_available, claude_cli_path, invoke,
)


PROMPT_SONNET = "Respond with exactly the word OK."
PROMPT_OPUS = "Respond with exactly the word READY."


def _open_ledger() -> sqlite3.Connection:
    db = REPO_ROOT / DEFAULT_LEDGER_PATH
    if not db.exists():
        print(f"ERROR: ledger not initialised at {db}; run tools/init_ledger.py",
              file=sys.stderr)
        sys.exit(2)
    conn = connect_writer(db)
    ensure_schema(conn)
    return conn


def _count_calls(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM llm_call_event"
    ).fetchone()[0])


def _smoke(role: str, prompt: str, conn: sqlite3.Connection) -> None:
    print(f"--- {role}: invoking via {claude_cli_path()} ...")
    before = _count_calls(conn)

    r1 = invoke(role=role, prompt=prompt, conn=conn)
    print(f"  call 1: model={r1.model} cache_hit={r1.cache_hit} "
          f"latency_ms={r1.latency_ms} "
          f"tokens_in={r1.input_tokens} out={r1.output_tokens}")
    print(f"  text   : {r1.text.strip()[:80]!r}")
    after1 = _count_calls(conn)
    assert after1 == before + 1, f"ledger row missing: {before} -> {after1}"
    assert not r1.cache_hit, "first call should not be a cache hit"

    r2 = invoke(role=role, prompt=prompt, conn=conn)
    print(f"  call 2: cache_hit={r2.cache_hit} (expect True)")
    after2 = _count_calls(conn)
    assert after2 == after1 + 1, "second call should still write a ledger row"
    assert r2.cache_hit, "second identical call should be a cache hit"
    assert r2.input_hash == r1.input_hash, "input hash should match"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-opus", action="store_true",
                        help="don't run the Opus leg")
    parser.add_argument("--skip-ledger", action="store_true",
                        help="skip ledger writes (offline mode)")
    args = parser.parse_args()

    if not claude_cli_available():
        print(f"ERROR: claude CLI not found at {claude_cli_path()!r}",
              file=sys.stderr)
        return 1

    conn = None if args.skip_ledger else _open_ledger()
    try:
        # Sonnet leg — uses scout_summarizer (P3, sonnet) so the ROLE_MODEL
        # mapping is exercised, but routed through invoke().
        _smoke("scout_summarizer", PROMPT_SONNET, conn)
        if not args.skip_opus:
            # Opus leg — strategy_implementer is mapped to opus.
            _smoke("strategy_implementer", PROMPT_OPUS, conn)
        print("OK: LLM transport smoke test passed.")
        return 0
    except (LLMUnavailable, AssertionError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
