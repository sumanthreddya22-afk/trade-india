"""Drift postmortem driver (v4 Phase A).

Bridge from a hash-chained event row (``drift_event`` /
``universe_audit_event`` / ``regime_event``) to a Claude memo
persisted in ``drift_postmortem_event``.

Wraps ``shared.llm_transport.invoke`` with the persona-hash check so
the kernel cannot accidentally call an un-anchored persona. Failure
modes (cache miss + budget exhausted + CLI down) degrade to a written
``_skip`` record rather than crashing the job.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ledger.drift_postmortem_event import write_event as write_pm
from trading_bot.research.persona_runner import (
    PersonaHashMismatch, verify_persona_hash,
)
from trading_bot.shared.llm_transport import LLMUnavailable, invoke

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _persona_path(persona_id: str) -> Path:
    return REPO_ROOT / "prompts" / "roles" / f"{persona_id}.v1.md"


def _hashes_path() -> Path:
    return REPO_ROOT / "policy" / "HASHES"


def _compose_prompt(persona_text: str, persona_hash: str, event_payload: Mapping) -> str:
    body = json.dumps(dict(event_payload), sort_keys=True,
                       separators=(",", ":"), default=str)
    return (
        f"# Persona (sha256:{persona_hash})\n\n{persona_text}\n\n"
        f"# Event\n\n```json\n{body}\n```\n\n"
        f"Respond with a single JSON object matching the persona's "
        f"required output schema. No prose before or after the JSON."
    )


def write_memo(
    conn: sqlite3.Connection,
    *,
    source_event_type: str,
    source_ledger_seq: int,
    event_payload: Mapping[str, Any],
    persona_id: str = "drift_postmortem",
) -> Optional[int]:
    """Compose the prompt, run Claude, and append a
    ``drift_postmortem_event`` row.

    Returns the new ledger_seq, or ``None`` when the call was skipped
    (cache exhausted / CLI down / persona hash mismatch). Skips are
    logged but do not raise — postmortem failure must never stop the
    daemon.
    """
    pp = _persona_path(persona_id)
    try:
        persona_hash = verify_persona_hash(pp, hashes_path=_hashes_path())
    except PersonaHashMismatch as e:
        log.error("drift_postmortem persona hash check failed: %s", e)
        return None

    persona_text = pp.read_text()
    prompt = _compose_prompt(persona_text, persona_hash, event_payload)

    try:
        resp = invoke(role=persona_id, prompt=prompt, conn=conn)
    except LLMUnavailable as e:
        log.info("drift_postmortem skipped: %s", e)
        return None

    # Try to extract a memo_markdown field; if the response wasn't JSON
    # the raw text becomes the memo.
    memo_md = resp.text
    try:
        parsed = json.loads(resp.text)
        if isinstance(parsed, dict) and parsed.get("memo_markdown"):
            memo_md = str(parsed["memo_markdown"])
    except json.JSONDecodeError:
        pass

    return write_pm(
        conn,
        source_event_type=source_event_type,
        source_ledger_seq=int(source_ledger_seq),
        persona_id=persona_id,
        persona_hash=f"sha256:{persona_hash}",
        memo_markdown=memo_md,
    )


__all__ = ["write_memo"]
