"""P0 acceptance: 8 persona files exist, each has the required schema fields
listed, and each declares forbidden_actions.

Phase 0 ships the persona content; the runtime hash-check enforcement
lands in Phase 2.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES = REPO_ROOT / "prompts" / "roles"

EXPECTED = [
    "quant_pm.v1.md",
    "quant_research_lead.v1.md",
    "risk_validator.v1.md",
    "trading_systems_engineer.v1.md",
    "execution_engineer.v1.md",
    "ai_mlops.v1.md",
    "sre_ops.v1.md",
    "compliance.v1.md",
]


def test_eight_personas_present() -> None:
    for name in EXPECTED:
        assert (ROLES / name).exists(), f"{name} missing"


def test_each_persona_declares_forbidden_actions() -> None:
    for name in EXPECTED:
        body = (ROLES / name).read_text()
        assert "forbidden_actions:" in body, f"{name} missing forbidden_actions block"


def test_each_persona_declares_required_output_schema() -> None:
    """Plan §1A required output: role / role_hash / subject_kind / subject_id /
    verdict / confidence / concerns / kill_conditions / grounding_refs / free_text.
    """
    fields = [
        '"role"',
        '"role_hash"',
        '"subject_kind"',
        '"subject_id"',
        '"verdict"',
        '"confidence"',
        '"concerns"',
        '"kill_conditions"',
        '"grounding_refs"',
        '"free_text"',
    ]
    for name in EXPECTED:
        body = (ROLES / name).read_text()
        missing = [f for f in fields if f not in body]
        assert not missing, f"{name} missing schema fields: {missing}"


def test_each_persona_carries_role_field_in_frontmatter() -> None:
    for name in EXPECTED:
        body = (ROLES / name).read_text()
        # YAML frontmatter starts with '---' on the first line.
        assert body.startswith("---\n"), f"{name} missing YAML frontmatter"
        m = re.search(r"^role:\s*(\S+)", body, flags=re.MULTILINE)
        assert m, f"{name} missing role: field in frontmatter"
        assert m.group(1).endswith(".v1"), (
            f"{name} role field should be persona.vN form, got {m.group(1)}"
        )
