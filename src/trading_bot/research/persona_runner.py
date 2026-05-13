"""Subprocess persona runner with hash check.

Plan v4 §1A: "L3 and L8 calls reference personas by hash; the persona
content embedded in the prompt is pinned at call time, and a hash
mismatch halts the call."

This runner reads the persona Markdown file, recomputes its SHA-256,
compares to the manifest in ``policy/HASHES``, refuses to proceed on
mismatch, then composes a prompt and spawns a subprocess (typically the
``claude`` CLI). The subprocess writes its JSON response on stdout; we
parse + validate it via ``persona_schema.validate_persona_output``.

For Phase 6 tests we accept a configurable ``runner_callable`` instead
of spawning a subprocess — this exercises the hash-check and parsing
paths without requiring a real LLM in CI.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from trading_bot.research.hypothesis_intake import HypothesisProposal
from trading_bot.research.persona_schema import validate_persona_output


REPO_ROOT = Path(__file__).resolve().parents[3]


class PersonaHashMismatch(Exception):
    """Raised when the persona file's sha256 doesn't match policy/HASHES."""


class PersonaInvocationError(Exception):
    """Raised when the subprocess fails or returns malformed output."""


def _parse_hashes_manifest(hashes_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in hashes_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        sha, rel = line.split(None, 1)
        out[rel.strip()] = sha
    return out


def verify_persona_hash(
    persona_path: Path, *, hashes_path: Path,
) -> str:
    """Recompute SHA-256 of the persona file; compare to manifest. Returns
    the verified hash on success; raises PersonaHashMismatch otherwise.
    """
    if not persona_path.exists():
        raise PersonaHashMismatch(f"persona file missing: {persona_path}")
    rel = str(persona_path.relative_to(REPO_ROOT)).replace("\\", "/")
    manifest = _parse_hashes_manifest(hashes_path)
    if rel not in manifest:
        raise PersonaHashMismatch(f"{rel} not in HASHES manifest")
    expected = manifest[rel]
    actual = hashlib.sha256(persona_path.read_bytes()).hexdigest()
    if actual != expected:
        raise PersonaHashMismatch(
            f"{rel}: expected {expected[:16]}..., got {actual[:16]}..."
        )
    return actual


SubprocessCallable = Callable[[str], str]
"""A callable that takes the composed prompt string and returns the raw
stdout (a JSON-encoded persona output). Phase 6 tests inject this."""


@dataclass(frozen=True)
class SubprocessPersonaRunner:
    role: str
    persona_path: Path
    hashes_path: Path = REPO_ROOT / "policy" / "HASHES"
    command: Sequence[str] = field(default_factory=lambda: ("claude", "--json"))
    runner_callable: Optional[SubprocessCallable] = None
    """Test seam — if provided, we call this instead of spawning the
    subprocess. The function signature is ``prompt_str -> stdout_str``."""

    def __call__(self, proposal: HypothesisProposal) -> Mapping[str, Any]:
        verified_hash = verify_persona_hash(
            self.persona_path, hashes_path=self.hashes_path,
        )
        prompt = self._compose_prompt(proposal, verified_hash)
        if self.runner_callable is not None:
            stdout = self.runner_callable(prompt)
        else:
            stdout = self._spawn(prompt)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise PersonaInvocationError(
                f"persona subprocess returned invalid JSON: {e}; stdout="
                f"{stdout[:200]}"
            ) from e
        ok, errs = validate_persona_output(payload)
        if not ok:
            raise PersonaInvocationError(
                f"persona output failed schema: {'; '.join(errs)}"
            )
        # Stamp the verified hash on the output so the caller can audit
        # which persona version this call ran against.
        payload["role_hash"] = f"sha256:{verified_hash}"
        return payload

    def _compose_prompt(
        self, proposal: HypothesisProposal, verified_hash: str,
    ) -> str:
        persona_text = self.persona_path.read_text()
        return (
            f"# Persona (sha256:{verified_hash})\n\n{persona_text}\n\n"
            f"# Proposal\n\nthesis_id: {proposal.thesis_id}\n"
            f"hypothesis_id: {proposal.hypothesis_id}\n"
            f"mechanism: {proposal.mechanism}\n"
            f"description: {proposal.description}\n"
            f"expected_regimes: {list(proposal.expected_regimes)}\n"
            f"kill_criteria: {list(proposal.kill_criteria)}\n\n"
            f"Respond with a single JSON object matching the persona's "
            f"required output schema. No prose before or after the JSON."
        )

    def _spawn(self, prompt: str) -> str:
        try:
            result = subprocess.run(
                list(self.command),
                input=prompt, capture_output=True, text=True,
                timeout=120, check=False,
            )
        except FileNotFoundError as e:
            raise PersonaInvocationError(
                f"persona subprocess not found: {self.command[0]}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise PersonaInvocationError(
                f"persona subprocess timed out: {self.command}"
            ) from e
        if result.returncode != 0:
            raise PersonaInvocationError(
                f"persona subprocess exit={result.returncode}: "
                f"{result.stderr[:200]}"
            )
        return result.stdout


__all__ = [
    "PersonaHashMismatch",
    "PersonaInvocationError",
    "SubprocessCallable",
    "SubprocessPersonaRunner",
    "verify_persona_hash",
]
