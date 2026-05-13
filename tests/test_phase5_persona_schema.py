"""Phase 5 — persona-output schema validator."""
from __future__ import annotations

from trading_bot.research import validate_persona_output


def _valid(verdict="support"):
    return {
        "role": "quant_research_lead.v1",
        "role_hash": "sha256:abcdef",
        "subject_kind": "thesis",
        "subject_id": "edge_thesis_v1",
        "verdict": verdict,
        "confidence": 0.6,
        "concerns": ["mechanism is plausible but trades persistence"],
        "kill_conditions": ["24m rolling Sharpe < 0"],
        "grounding_refs": ["thesis:edge_thesis_v1"],
        "free_text": "ok",
    }


def test_valid_payload() -> None:
    ok, errs = validate_persona_output(_valid())
    assert ok, errs


def test_missing_field() -> None:
    p = _valid()
    p.pop("verdict")
    ok, errs = validate_persona_output(p)
    assert not ok
    assert any("verdict" in e for e in errs)


def test_bad_role_format() -> None:
    p = _valid()
    p["role"] = "quant_research_lead"
    ok, errs = validate_persona_output(p)
    assert not ok


def test_bad_subject_kind() -> None:
    p = _valid()
    p["subject_kind"] = "ghost"
    ok, errs = validate_persona_output(p)
    assert not ok


def test_bad_confidence_out_of_range() -> None:
    p = _valid()
    p["confidence"] = 1.5
    ok, errs = validate_persona_output(p)
    assert not ok


def test_grounding_refs_must_be_nonempty() -> None:
    p = _valid()
    p["grounding_refs"] = []
    ok, errs = validate_persona_output(p)
    assert not ok
    assert any("grounding_refs" in e for e in errs)
