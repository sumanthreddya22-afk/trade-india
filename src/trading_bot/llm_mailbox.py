"""LLM mailbox — file-based queue between the trading-bot daemon and a
Claude Code scheduled routine, so latency-tolerant LLM work bills under
the user's Claude Code subscription instead of the API key.

Architecture:

    daemon:                            scheduled Claude Code routine:
    -------                            -------------------------------
    MailboxQueue.submit(brief)  -->    pending/<id>.json
                                       |
                                       | routine fires every N min,
                                       | reads pending, reasons, writes
                                       v
    MailboxQueue.poll(id, t) <--       done/<id>.json
                                       |
                                       v
                                       processed/<id>.json (for audit)

Brief schema (pending/<id>.json):
    {
      "id": "<uuid>",
      "version": 1,
      "role": "decision_reflector",            # which trading_bot role
      "model_class": "reflector",              # routine hint: judge|debater|reflector|architect
      "system": "...",                          # system prompt
      "messages": [{"role":"user","content":"..."}],
      "max_tokens": 400,
      "tool": {                                # optional structured-output schema
         "name": "record_lesson",
         "description": "...",
         "schema": { ... }                     # JSONSchema for the tool input
      },
      "submitted_at_utc": "2026-05-01T23:11:00+00:00",
      "deadline_utc":     "2026-05-02T07:00:00+00:00"
    }

Result schema (done/<id>.json):
    {
      "id": "<same id>",
      "completed_at_utc": "2026-05-01T23:13:11+00:00",
      "model_used": "claude-opus-4-7",        # routine fills in actual model
      "text": "...",                            # free-text content
      "structured": { ... } | null,             # parsed tool input if tool was supplied
      "input_tokens":  null | int,              # routine may not be able to fill these
      "output_tokens": null | int,
      "error":  null | "string"                 # set when the routine failed to process
    }

Failure modes:
  * Routine doesn't fire within the deadline → poll() returns None, caller
    falls back to direct AnthropicClient call.
  * Result is corrupt JSON → moved to failed/, poll() returns None.
  * Brief is unparseable → stays in pending/ until cleanup.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
import uuid
from pathlib import Path
from typing import Any


_DEFAULT_BASE = Path("data/llm_queue")
_BRIEF_VERSION = 1
_POLL_INTERVAL_SECONDS = 1.0


@dataclasses.dataclass(frozen=True)
class Brief:
    role: str
    model_class: str          # "judge" | "debater" | "reflector" | "architect"
    system: str
    messages: list[dict]
    max_tokens: int = 4096
    tool_name: str | None = None
    tool_description: str | None = None
    tool_schema: dict | None = None
    deadline_seconds: int = 1800   # 30 min default

    def to_payload(self, *, brief_id: str) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        deadline = now + dt.timedelta(seconds=self.deadline_seconds)
        out: dict = {
            "id": brief_id,
            "version": _BRIEF_VERSION,
            "role": self.role,
            "model_class": self.model_class,
            "system": self.system,
            "messages": self.messages,
            "max_tokens": self.max_tokens,
            "submitted_at_utc": now.isoformat(),
            "deadline_utc": deadline.isoformat(),
        }
        if self.tool_name is not None:
            out["tool"] = {
                "name": self.tool_name,
                "description": self.tool_description or "",
                "schema": self.tool_schema or {},
            }
        return out


@dataclasses.dataclass(frozen=True)
class Result:
    id: str
    completed_at_utc: dt.datetime
    text: str
    structured: dict | None
    model_used: str | None
    input_tokens: int | None
    output_tokens: int | None
    error: str | None

    @property
    def used_structured(self) -> bool:
        return self.structured is not None


class MailboxQueue:
    """File-backed mailbox under data/llm_queue/.

    Thread-safety: relies on filesystem-atomic os.rename moves between
    pending/done/processed/failed. No locking — the daemon is the only
    submitter and a single routine instance is the only processor.
    """

    def __init__(self, base: str | Path = _DEFAULT_BASE) -> None:
        self.base = Path(base)
        for sub in ("pending", "done", "processed", "failed"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Submit / poll
    # ------------------------------------------------------------------

    def submit(self, brief: Brief) -> str:
        """Write a brief to pending/. Returns the brief id."""
        brief_id = self._new_id(brief.role)
        payload = brief.to_payload(brief_id=brief_id)
        path = self._pending_path(brief_id)
        # Write to a tmp file then rename for atomicity.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, default=str))
        tmp.rename(path)
        return brief_id

    def poll(self, brief_id: str, *, timeout_seconds: float) -> Result | None:
        """Wait up to `timeout_seconds` for done/<brief_id>.json to appear.

        Returns the parsed Result on success. Returns None if the deadline
        passes (caller should fall back to direct API). On corrupt JSON,
        moves the result to failed/ and returns None.
        """
        deadline = time.monotonic() + timeout_seconds
        done = self._done_path(brief_id)
        while time.monotonic() < deadline:
            if done.exists():
                return self._consume_done(brief_id)
            time.sleep(_POLL_INTERVAL_SECONDS)
        return None

    def submit_and_wait(
        self, brief: Brief, *, timeout_seconds: float
    ) -> Result | None:
        bid = self.submit(brief)
        return self.poll(bid, timeout_seconds=timeout_seconds)

    # ------------------------------------------------------------------
    # Routine-side helpers (called from the scheduled task)
    # ------------------------------------------------------------------

    def list_pending_briefs(self) -> list[dict]:
        """Read every pending/*.json in submission order. Skips unparseable
        files (moves them to failed/). Used by the routine to discover work.
        """
        out: list[dict] = []
        for path in sorted((self.base / "pending").glob("*.json")):
            try:
                payload = json.loads(path.read_text())
                out.append(payload)
            except Exception as e:
                self._move_failed(path, reason=f"unparseable_brief: {e}")
        return out

    def write_result(self, brief_id: str, *, result: dict) -> None:
        """Routine writes the result to done/<brief_id>.json AND moves the
        brief from pending/ → processed/ for audit.
        """
        done_path = self._done_path(brief_id)
        tmp = done_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, default=str))
        tmp.rename(done_path)

        pending = self._pending_path(brief_id)
        if pending.exists():
            (self.base / "processed" / pending.name).write_text(pending.read_text())
            pending.unlink()

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def cleanup_old(self, *, days: int = 14) -> int:
        """Delete processed/ and failed/ files older than `days`. Returns
        the number deleted."""
        cutoff = time.time() - days * 86400
        n = 0
        for sub in ("processed", "failed", "done"):
            for path in (self.base / sub).glob("*.json"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        n += 1
                except OSError:
                    pass
        return n

    def stats(self) -> dict[str, int]:
        return {
            "pending":   sum(1 for _ in (self.base / "pending").glob("*.json")),
            "done":      sum(1 for _ in (self.base / "done").glob("*.json")),
            "processed": sum(1 for _ in (self.base / "processed").glob("*.json")),
            "failed":    sum(1 for _ in (self.base / "failed").glob("*.json")),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _new_id(self, role: str) -> str:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{ts}-{role}-{uuid.uuid4().hex[:8]}"

    def _pending_path(self, brief_id: str) -> Path:
        return self.base / "pending" / f"{brief_id}.json"

    def _done_path(self, brief_id: str) -> Path:
        return self.base / "done" / f"{brief_id}.json"

    def _consume_done(self, brief_id: str) -> Result | None:
        done = self._done_path(brief_id)
        try:
            raw = json.loads(done.read_text())
        except Exception as e:
            self._move_failed(done, reason=f"unparseable_result: {e}")
            return None

        try:
            completed = dt.datetime.fromisoformat(str(raw.get("completed_at_utc")))
        except Exception:
            completed = dt.datetime.now(dt.timezone.utc)

        result = Result(
            id=str(raw.get("id") or brief_id),
            completed_at_utc=completed,
            text=str(raw.get("text") or ""),
            structured=raw.get("structured"),
            model_used=raw.get("model_used"),
            input_tokens=_safe_int(raw.get("input_tokens")),
            output_tokens=_safe_int(raw.get("output_tokens")),
            error=raw.get("error"),
        )
        # Move done/ → processed/ for audit retention.
        try:
            (self.base / "processed" / done.name).write_text(done.read_text())
            done.unlink()
        except OSError:
            pass
        return result

    def _move_failed(self, path: Path, *, reason: str) -> None:
        try:
            target = self.base / "failed" / path.name
            target.write_text(path.read_text() + f"\n# moved_to_failed: {reason}\n")
            path.unlink()
        except OSError:
            pass


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
