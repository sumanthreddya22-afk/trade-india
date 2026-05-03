"""Cross-pipeline CI lint — surfaces drift in tests + conventions.

Runs on every PR. Pure static analysis (no LLM cost).

Three concerns:
1. **Test taxonomy** — every pipeline must have tests for the categories
   in ``tests/_taxonomy.md`` (happy path, fail-soft, stale-state abort,
   broker-reject, persona inventory, migrations). Missing categories are
   warnings (not errors) — drift may be intentional but always visible.
2. **Persona inventory** — every persona file under ``shared/personas/``
   or ``pipelines/<asset>/personas/`` must declare a valid PERSONA dict
   per ``shared/personas/_base.py``.
3. **ADR coverage** — when a PR adds a new top-level key under
   ``strategy/config.yaml`` and there's no matching ``docs/adrs/`` file
   touched, warn.

Returns exit code 0 (warnings only). Pass ``--strict`` to fail on
warnings — useful for hard CI gates.

Run as: ``uv run python -m trading_bot.shared.automation.ci_lint [--strict]``
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Iterable, List

PIPELINES = ("stocks", "crypto", "options")
PIPELINE_ROOT = Path("src/trading_bot/pipelines")
SHARED_PERSONAS_PKG = "trading_bot.shared.personas"

REQUIRED_TEST_PATTERNS = (
    ("happy_path",         "test_{pipeline}_happy_path.py"),
    ("fail_soft_llm",      "test_{pipeline}_fail_soft_llm.py"),
    ("stale_verdict",      "test_{pipeline}_stale_verdict_abort.py"),
    ("broker_reject",      "test_{pipeline}_broker_reject.py"),
    ("persona_inventory",  "test_{pipeline}_persona_inventory.py"),
    ("migrations",         "test_{pipeline}_migrations.py"),
)


def _ensure_repo_root_on_path() -> None:
    src = Path("src").resolve()
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def lint_test_taxonomy() -> List[str]:
    """One warning per (pipeline, missing-test-category)."""
    warnings: List[str] = []
    for pipeline in PIPELINES:
        pipe_root = PIPELINE_ROOT / pipeline
        if not pipe_root.exists():
            # Pipeline not yet built; skip rather than spam warnings.
            continue
        tests_dir = pipe_root / "tests"
        if not tests_dir.exists():
            warnings.append(
                f"[test-taxonomy] {pipeline}: pipelines/{pipeline}/tests/ does not exist yet"
            )
            continue
        existing = {p.name for p in tests_dir.glob("*.py")}
        for category, pattern in REQUIRED_TEST_PATTERNS:
            expected = pattern.format(pipeline=pipeline)
            if expected not in existing:
                warnings.append(
                    f"[test-taxonomy] {pipeline}: missing {category} test "
                    f"({tests_dir / expected}) — see tests/_taxonomy.md"
                )
    return warnings


def _persona_dirs() -> Iterable[Path]:
    yield Path("src/trading_bot/shared/personas")
    for pipeline in PIPELINES:
        candidate = PIPELINE_ROOT / pipeline / "personas"
        if candidate.exists():
            yield candidate


def lint_persona_inventory() -> List[str]:
    """One warning per persona file that fails to validate."""
    _ensure_repo_root_on_path()
    try:
        from trading_bot.shared.personas._base import (
            parse, PersonaSchemaError,
        )
    except Exception as e:  # pragma: no cover — _base import shouldn't fail
        return [f"[persona-inventory] cannot import _base: {e}"]

    warnings: List[str] = []
    for persona_dir in _persona_dirs():
        for path in sorted(persona_dir.glob("*.py")):
            stem = path.stem
            if stem.startswith("_") or stem == "__init__":
                continue
            module_name = (
                str(path.with_suffix(""))
                .replace("/", ".")
                .replace("src.", "", 1)
            )
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                warnings.append(
                    f"[persona-inventory] {path}: import failed: {type(e).__name__}: {e}"
                )
                continue
            persona_dict = getattr(module, "PERSONA", None)
            if persona_dict is None:
                warnings.append(
                    f"[persona-inventory] {path}: missing top-level PERSONA dict"
                )
                continue
            try:
                parse(persona_dict)
            except PersonaSchemaError as e:
                warnings.append(
                    f"[persona-inventory] {path}: invalid PERSONA: {e}"
                )
    return warnings


def lint_adr_coverage() -> List[str]:
    """Sanity-check that ``docs/adrs/`` is non-empty and has a README.

    A more sophisticated check (config-key vs ADR diff) wires up later
    once PR diffs are available in CI context.
    """
    warnings: List[str] = []
    adr_root = Path("docs/adrs")
    if not adr_root.exists():
        warnings.append("[adr-coverage] docs/adrs/ directory does not exist")
        return warnings
    if not (adr_root / "README.md").exists():
        warnings.append("[adr-coverage] docs/adrs/README.md missing")
    adr_files = [p for p in adr_root.glob("*.md") if p.name != "README.md"]
    if not adr_files:
        warnings.append("[adr-coverage] no ADR files in docs/adrs/ yet")
    return warnings


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any warning is emitted",
    )
    args = parser.parse_args(argv)

    warnings = (
        lint_test_taxonomy()
        + lint_persona_inventory()
        + lint_adr_coverage()
    )

    for w in warnings:
        print(w)

    if not warnings:
        print("[ci-lint] OK — no drift detected")
        return 0

    print(f"[ci-lint] {len(warnings)} warning(s) emitted")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
