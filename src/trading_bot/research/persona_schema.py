"""Validator for persona-runner outputs.

Plan v4 §1A: every persona call MUST return JSON matching this shape:

    {
      "role": "<persona>.v1",
      "role_hash": "sha256:...",
      "subject_kind": "thesis | strategy_version | incident | daily_report",
      "subject_id": "...",
      "verdict": "support | block | abstain",
      "confidence": 0.0,
      "concerns": ["..."],
      "kill_conditions": ["..."],
      "grounding_refs": ["ledger_seq:1234", "feature:..."],
      "free_text": "..."
    }

The runner (Phase 5 mock; Phase 6 real) is responsible for verifying
each ``grounding_refs`` entry points to a real ledger row. This module
ships the structural validator only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

ALLOWED_VERDICTS = frozenset({"support", "block", "abstain"})
ALLOWED_SUBJECT_KINDS = frozenset({
    "thesis", "strategy_version", "incident", "daily_report",
    "live_readiness_packet",
})

REQUIRED_FIELDS = (
    "role", "role_hash", "subject_kind", "subject_id",
    "verdict", "confidence", "concerns", "kill_conditions",
    "grounding_refs", "free_text",
)


@dataclass(frozen=True)
class PersonaOutputError(Exception):
    """Single-field validation error."""

    field: str
    reason: str

    def __str__(self) -> str:                # pragma: no cover - trivial
        return f"persona_output:{self.field}: {self.reason}"


def validate_persona_output(payload: Mapping[str, Any]) -> Tuple[bool, list[str]]:
    """Returns ``(valid, errors)``.

    ``errors`` is a list of human-readable strings — empty when valid.
    """
    errors: list[str] = []

    for f in REQUIRED_FIELDS:
        if f not in payload:
            errors.append(f"missing field: {f}")

    if errors:
        return False, errors

    if not isinstance(payload["role"], str) or not payload["role"].endswith(".v1"):
        errors.append("role must be a string ending in '.v1'")
    if not isinstance(payload["role_hash"], str) or not payload["role_hash"].startswith("sha256:"):
        errors.append("role_hash must start with 'sha256:'")
    if payload["subject_kind"] not in ALLOWED_SUBJECT_KINDS:
        errors.append(f"subject_kind must be one of {sorted(ALLOWED_SUBJECT_KINDS)}")
    if not isinstance(payload["subject_id"], str) or not payload["subject_id"]:
        errors.append("subject_id must be a non-empty string")
    if payload["verdict"] not in ALLOWED_VERDICTS:
        errors.append(f"verdict must be one of {sorted(ALLOWED_VERDICTS)}")
    conf = payload["confidence"]
    if not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
        errors.append("confidence must be a number in [0.0, 1.0]")
    for f in ("concerns", "kill_conditions", "grounding_refs"):
        if not isinstance(payload[f], list):
            errors.append(f"{f} must be a list")
    if isinstance(payload.get("grounding_refs"), list) and not payload["grounding_refs"]:
        errors.append("grounding_refs must contain at least one ref")
    if not isinstance(payload.get("free_text", ""), str):
        errors.append("free_text must be a string")

    return (not errors), errors


__all__ = [
    "ALLOWED_SUBJECT_KINDS",
    "ALLOWED_VERDICTS",
    "REQUIRED_FIELDS",
    "PersonaOutputError",
    "validate_persona_output",
]
