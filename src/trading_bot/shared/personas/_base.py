"""Persona file format — single source of truth for all LLM personas.

Every LLM-using point in any pipeline MUST have a versioned persona file
declaring a ``PERSONA`` dict matching the schema below. The dashboard,
audit log, lesson loop, and drift detector all read this dict directly,
so the metadata is the canonical UX label as well as the prompt source.

Keeping this in ``shared/`` means stocks, crypto, and options personas
all conform to the same structure — even though their prompt content
diverges freely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Persona:
    """Structured representation of a persona, parsed from a module's PERSONA dict.

    Fields are deliberately small and explicit. The dashboard reads these
    fields directly to render debate transcripts and the Trading Desk
    Roster page, so changes here ripple to UI without further wiring.
    """

    id: str
    full_name: str
    role_title: str
    years_experience: int
    firm_pedigree: str
    specialties: List[str]
    default_stance: str
    pipeline: str          # "stocks" | "crypto" | "options" | "shared"
    debate_role: str       # e.g. "scout_skeptic", "hold_judge", "lesson_analyst"
    model_tier: str        # "judge" | "reviewer" | "classifier" | "summary"
    prompt_template: str
    prompt_version: str = "v1"
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------- format requirements (used by validators below) -------------------

REQUIRED_KEYS = (
    "id",
    "full_name",
    "role_title",
    "years_experience",
    "firm_pedigree",
    "specialties",
    "default_stance",
    "pipeline",
    "debate_role",
    "model_tier",
    "prompt_template",
)

VALID_PIPELINES = frozenset({"stocks", "crypto", "options", "shared"})

VALID_MODEL_TIERS = frozenset({"judge", "reviewer", "classifier", "summary"})


class PersonaSchemaError(ValueError):
    """Raised when a PERSONA dict is missing fields or has invalid values."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PersonaSchemaError(message)


def parse(persona_dict: Dict[str, Any]) -> Persona:
    """Validate a PERSONA dict and return a Persona dataclass.

    Raises PersonaSchemaError on any structural problem. Strict by design:
    every field is required so the dashboard never has to deal with
    partial data.
    """
    for key in REQUIRED_KEYS:
        _require(key in persona_dict, f"PERSONA missing required key: {key!r}")

    pipeline = persona_dict["pipeline"]
    _require(
        pipeline in VALID_PIPELINES,
        f"PERSONA.pipeline must be one of {sorted(VALID_PIPELINES)}, got {pipeline!r}",
    )

    model_tier = persona_dict["model_tier"]
    _require(
        model_tier in VALID_MODEL_TIERS,
        f"PERSONA.model_tier must be one of {sorted(VALID_MODEL_TIERS)}, got {model_tier!r}",
    )

    specialties = persona_dict["specialties"]
    _require(
        isinstance(specialties, (list, tuple)) and all(isinstance(s, str) for s in specialties),
        "PERSONA.specialties must be a list of strings",
    )

    years = persona_dict["years_experience"]
    _require(
        isinstance(years, int) and years >= 0,
        "PERSONA.years_experience must be a non-negative int",
    )

    full_name = persona_dict["full_name"]
    _require(
        isinstance(full_name, str) and " " in full_name.strip(),
        "PERSONA.full_name must be 'First Last' (invented human-sounding name)",
    )

    prompt_template = persona_dict["prompt_template"]
    _require(
        isinstance(prompt_template, str) and len(prompt_template.strip()) > 100,
        "PERSONA.prompt_template must be a non-trivial string (>100 chars)",
    )

    return Persona(
        id=str(persona_dict["id"]),
        full_name=str(full_name).strip(),
        role_title=str(persona_dict["role_title"]).strip(),
        years_experience=int(years),
        firm_pedigree=str(persona_dict["firm_pedigree"]).strip(),
        specialties=list(specialties),
        default_stance=str(persona_dict["default_stance"]).strip(),
        pipeline=pipeline,
        debate_role=str(persona_dict["debate_role"]).strip(),
        model_tier=model_tier,
        prompt_template=prompt_template,
        prompt_version=str(persona_dict.get("prompt_version", "v1")),
        extra=dict(persona_dict.get("extra", {})),
    )


# ---------- runtime helpers --------------------------------------------------


def render_prompt(persona: Persona, **substitutions: Any) -> str:
    """Render the persona's prompt template with substitutions.

    Uses Python str.format so persona templates can include {symbol},
    {brief}, {prior_text}, etc. Missing substitutions raise KeyError —
    we want loud failures, not silent template artifacts in prompts.
    """
    return persona.prompt_template.format(**substitutions)


def display_label(persona: Persona, *, with_years: bool = True) -> str:
    """Return the dashboard label for this persona.

    Example: "Sasha Volkov \xb7 On-Chain Forensic Analyst, 8yr"
    """
    if with_years:
        return f"{persona.full_name} · {persona.role_title}, {persona.years_experience}yr"
    return f"{persona.full_name} · {persona.role_title}"


# ---------- discovery --------------------------------------------------------


def load_from_module(module: Any) -> Persona:
    """Load a Persona from an imported module exposing a top-level PERSONA dict."""
    persona_dict = getattr(module, "PERSONA", None)
    _require(
        isinstance(persona_dict, dict),
        f"Module {module!r} is missing a top-level PERSONA dict",
    )
    return parse(persona_dict)


def discover(package: Any) -> List[Persona]:
    """Return all Persona objects defined in modules of ``package``.

    Used by the dashboard's Trading Desk Roster page and by the
    persona-inventory CI lint. Skips modules without a PERSONA dict
    (so __init__.py and helpers don't trip the loader).
    """
    import importlib
    import pkgutil

    personas: List[Persona] = []
    for module_info in pkgutil.iter_modules(package.__path__, prefix=f"{package.__name__}."):
        if module_info.name.rsplit(".", 1)[-1].startswith("_"):
            continue  # skip _base, __init__, etc.
        module = importlib.import_module(module_info.name)
        persona_dict = getattr(module, "PERSONA", None)
        if isinstance(persona_dict, dict):
            personas.append(parse(persona_dict))
    return personas


def grouped_by_role(personas: Iterable[Persona]) -> Dict[str, Persona]:
    """Index personas by their debate_role for quick lookup at debate time."""
    by_role: Dict[str, Persona] = {}
    for p in personas:
        by_role[p.debate_role] = p
    return by_role
