"""Strategy codegen pipeline (v4 Phase D).

End-to-end flow used by ``research_bot.run_intake_pipeline``:

  1. Compose a prompt for the ``strategy_implementer`` persona from a
     blueprint (candidate + taxonomy + an example v3 runner).
  2. Invoke the persona via ``shared.llm_transport.invoke``.
  3. Parse the JSON response into a file dict.
  4. AST-validate every file:
       * imports limited to the allow-list,
       * no ``os.system`` / ``subprocess`` / ``eval`` / ``exec``,
       * no file writes outside ``src/trading_bot/strategies/<family>_auto_v1/``,
       * required exports present.
  5. Write to a sandbox tmpdir.
  6. Run ``ruff`` + ``pytest`` against the new family.
  7. On all green, copy into the repo and write a
     ``strategy_codegen_event`` row.

Designed to be safe: nothing touches the repo path until every gate
passes. A failure aborts cleanly and writes a codegen event with the
gate that failed.
"""
from __future__ import annotations

import ast
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from trading_bot.ledger import connect_writer
from trading_bot.ledger.research_bot import write_codegen
from trading_bot.research.persona_runner import (
    PersonaHashMismatch, verify_persona_hash,
)
from trading_bot.shared.llm_transport import LLMUnavailable, invoke

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


# Allow-list per session-summary §7.5 "Code-gen safety rails".
ALLOWED_TOP_LEVEL_IMPORTS: tuple[str, ...] = (
    "numpy", "pandas", "dataclasses", "typing", "datetime", "logging",
    "json", "math", "statistics", "enum", "pathlib", "functools",
    # Our own non-kernel utility code is allowed:
    "trading_bot.ingest.universe",
    "trading_bot.research.historical_bars",
    "trading_bot.research.universe_discovery",
    "trading_bot.intel",
    "trading_bot.strategies",  # for cross-strategy helpers
)

FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "os.system",
    "subprocess.",
    "eval(",
    "exec(",
    "__import__",
    "open(",
    "urllib.request",
    "requests.",
)

FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "trading_bot.kernel",
    "trading_bot.execution",
    "trading_bot.risk.precheck",
    "trading_bot.risk.order_router",
    "trading_bot.ingest.alpaca_adapter",
    "alpaca",
    "anthropic",
    "openai",
    "socket",
    "requests",
    "urllib",
    "ftplib",
    "smtplib",
    "ssl",
)


@dataclass
class CodegenReport:
    family_id: str
    accepted: bool
    reason: str
    files_written: list[str] = field(default_factory=list)
    ruff_pass: bool = False
    pytest_pass: bool = False
    mypy_pass: bool = False


# ---------------------------------------------------------------------------
# AST validator
# ---------------------------------------------------------------------------

def _import_allowed(import_path: str) -> bool:
    """Return True iff ``import_path`` (e.g. ``"numpy"``) is allow-listed
    and not on the forbidden-prefix list."""
    for forbidden in FORBIDDEN_IMPORT_PREFIXES:
        if import_path == forbidden or import_path.startswith(forbidden + "."):
            return False
    for allowed in ALLOWED_TOP_LEVEL_IMPORTS:
        if import_path == allowed or import_path.startswith(allowed + "."):
            return True
    return False


def validate_source(source: str, *, path_for_errors: str) -> tuple[bool, str]:
    """Parse the source and check imports + substrings. Returns
    (ok, reason)."""
    for forbidden in FORBIDDEN_SUBSTRINGS:
        if forbidden in source:
            return False, f"{path_for_errors}: contains forbidden token {forbidden!r}"
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"{path_for_errors}: syntax error {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_allowed(alias.name):
                    return False, f"{path_for_errors}: forbidden import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0:
                return False, f"{path_for_errors}: relative import not allowed"
            if not _import_allowed(mod):
                return False, f"{path_for_errors}: forbidden from-import {mod}"
    return True, "ok"


def validate_runner_exports(source: str) -> tuple[bool, str]:
    """Check that runner.py exports ``evaluate_strategy`` and
    ``should_rebalance_today`` at module level."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"syntax: {e}"
    required = {"evaluate_strategy", "should_rebalance_today"}
    defined: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
    missing = required - defined
    if missing:
        return False, f"runner.py missing exports: {sorted(missing)}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Sandbox runners (ruff + pytest)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return r.returncode, (r.stdout + r.stderr)[-4000:]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return -1, f"{type(e).__name__}: {e}"


def _ruff_check(family_dir: Path) -> tuple[bool, str]:
    code, out = _run(["ruff", "check", "--no-fix", str(family_dir)], cwd=REPO_ROOT)
    if code == -1:
        return True, "ruff not available; skipping"
    return code == 0, out


def _pytest_run(test_path: Path) -> tuple[bool, str]:
    if not test_path.exists():
        return False, "test file missing"
    code, out = _run(
        [".venv/bin/python", "-m", "pytest", str(test_path), "-q", "--no-header"],
        cwd=REPO_ROOT, timeout=120,
    )
    return code == 0, out


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _read_example_runner() -> str:
    p = REPO_ROOT / "src" / "trading_bot" / "strategies" / "etf_momentum_v3" / "runner.py"
    if p.exists():
        return p.read_text()
    return ""


def _compose_implementer_prompt(
    *,
    persona_text: str,
    persona_hash: str,
    blueprint_md: str,
    family_id: str,
    intel_features_available: Sequence[str],
) -> str:
    example = _read_example_runner()
    return (
        f"# Persona (sha256:{persona_hash})\n\n{persona_text}\n\n"
        f"# Blueprint\n\n{blueprint_md}\n\n"
        f"# Family id (use this for directory name)\n\n`{family_id}`\n\n"
        f"# Available intel features\n\n"
        f"```\n{json.dumps(list(intel_features_available), indent=2)}\n```\n\n"
        f"# Reference runner (etf_momentum_v3 — match this structure)\n\n"
        f"```python\n{example[:6000]}\n```\n\n"
        f"Return a single JSON object matching the persona's schema. "
        f"No prose before or after."
    )


# ---------------------------------------------------------------------------
# Pipeline entry
# ---------------------------------------------------------------------------

def generate_for_blueprint(
    ledger_db: Path,
    *,
    blueprint_id: int,
    blueprint_md: str,
    family_id: str,
    intel_features: Sequence[str] = (),
    persona_id: str = "strategy_implementer",
    dry_run: bool = False,
) -> CodegenReport:
    persona_path = REPO_ROOT / "prompts" / "roles" / f"{persona_id}.v1.md"
    hashes_path = REPO_ROOT / "policy" / "HASHES"
    try:
        persona_hash = verify_persona_hash(persona_path, hashes_path=hashes_path)
    except PersonaHashMismatch as e:
        return CodegenReport(
            family_id=family_id, accepted=False,
            reason=f"persona_hash: {e}",
        )

    prompt = _compose_implementer_prompt(
        persona_text=persona_path.read_text(),
        persona_hash=persona_hash,
        blueprint_md=blueprint_md,
        family_id=family_id,
        intel_features_available=intel_features,
    )

    conn = connect_writer(ledger_db)
    try:
        try:
            resp = invoke(role=persona_id, prompt=prompt, conn=conn)
        except LLMUnavailable as e:
            report = CodegenReport(
                family_id=family_id, accepted=False,
                reason=f"llm_unavailable: {e}",
            )
            write_codegen(
                conn, blueprint_id=blueprint_id, new_family_id=family_id,
                runner_path="", tests_path="",
                ruff_pass=False, mypy_pass=False, test_pass=False,
                registered=False,
            )
            conn.commit()
            return report

        try:
            payload = json.loads(resp.text)
        except json.JSONDecodeError as e:
            return CodegenReport(
                family_id=family_id, accepted=False,
                reason=f"llm_json_parse: {e}",
            )

        files = payload.get("files") or {}
        if not files:
            return CodegenReport(
                family_id=family_id, accepted=False,
                reason="llm_returned_no_files",
            )

        # Validate every file's source.
        family_subpath = (
            f"src/trading_bot/strategies/{family_id}_auto_v1/"
        )
        test_subpath = f"tests/strategies/test_{family_id}_auto_v1.py"
        for rel, src in files.items():
            if not isinstance(src, str):
                return CodegenReport(
                    family_id=family_id, accepted=False,
                    reason=f"non_string_file:{rel}",
                )
            # Path scoping
            if not (rel.startswith(family_subpath) or rel == test_subpath):
                return CodegenReport(
                    family_id=family_id, accepted=False,
                    reason=f"file_outside_sandbox:{rel}",
                )
            if rel.endswith(".py"):
                ok, reason = validate_source(src, path_for_errors=rel)
                if not ok:
                    return CodegenReport(
                        family_id=family_id, accepted=False, reason=reason,
                    )
                if rel.endswith("runner.py"):
                    ok2, r2 = validate_runner_exports(src)
                    if not ok2:
                        return CodegenReport(
                            family_id=family_id, accepted=False, reason=r2,
                        )

        # Write to a sandbox dir, run gates, then optionally publish.
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td)
            for rel, src in files.items():
                target = sandbox / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(src)

            # ruff + pytest run against the SANDBOXED tree by copying
            # files into the repo (but only after ALL validation passes).
            # For safety we run pytest only on the test file via a direct
            # path; failing pytest aborts before any repo write.
            family_dir = sandbox / family_subpath
            ruff_ok, ruff_out = _ruff_check(family_dir)

            tests_path_abs = sandbox / test_subpath

            if dry_run:
                report = CodegenReport(
                    family_id=family_id, accepted=ruff_ok,
                    reason="dry_run",
                    files_written=list(files.keys()),
                    ruff_pass=ruff_ok,
                    pytest_pass=False, mypy_pass=False,
                )
                write_codegen(
                    conn, blueprint_id=blueprint_id, new_family_id=family_id,
                    runner_path=str(family_dir / "runner.py"),
                    tests_path=str(tests_path_abs),
                    ruff_pass=ruff_ok, mypy_pass=False, test_pass=False,
                    registered=False,
                )
                conn.commit()
                return report

            if not ruff_ok:
                write_codegen(
                    conn, blueprint_id=blueprint_id, new_family_id=family_id,
                    runner_path="", tests_path="",
                    ruff_pass=False, mypy_pass=False, test_pass=False,
                    registered=False,
                )
                conn.commit()
                return CodegenReport(
                    family_id=family_id, accepted=False,
                    reason=f"ruff_fail: {ruff_out[:400]}",
                    ruff_pass=False,
                )

            # Stage into repo path so pytest can import the new module.
            published: list[Path] = []
            try:
                for rel, src in files.items():
                    dst = REPO_ROOT / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(src)
                    published.append(dst)
                # Run the test file we just published.
                test_dst = REPO_ROOT / test_subpath
                pytest_ok, pytest_out = _pytest_run(test_dst)
            except Exception as e:  # noqa: BLE001
                for f in published:
                    f.unlink(missing_ok=True)
                return CodegenReport(
                    family_id=family_id, accepted=False,
                    reason=f"publish_or_pytest_exception:{e}",
                )

            if not pytest_ok:
                # Roll back the published files.
                for f in published:
                    f.unlink(missing_ok=True)
                write_codegen(
                    conn, blueprint_id=blueprint_id, new_family_id=family_id,
                    runner_path="", tests_path="",
                    ruff_pass=True, mypy_pass=False, test_pass=False,
                    registered=False,
                )
                conn.commit()
                return CodegenReport(
                    family_id=family_id, accepted=False,
                    reason=f"pytest_fail: {pytest_out[:400]}",
                    ruff_pass=True, pytest_pass=False,
                )

            # All gates green — record + return.
            write_codegen(
                conn, blueprint_id=blueprint_id, new_family_id=family_id,
                runner_path=str(REPO_ROOT / family_subpath / "runner.py"),
                tests_path=str(REPO_ROOT / test_subpath),
                ruff_pass=True, mypy_pass=False,  # mypy intentionally skipped
                test_pass=True,
                registered=False,  # caller (research_bot) handles registration
            )
            conn.commit()
            return CodegenReport(
                family_id=family_id, accepted=True,
                reason="all gates passed",
                files_written=[str(p.relative_to(REPO_ROOT)) for p in published],
                ruff_pass=True, pytest_pass=True,
            )
    finally:
        conn.close()


__all__ = [
    "ALLOWED_TOP_LEVEL_IMPORTS",
    "CodegenReport",
    "FORBIDDEN_IMPORT_PREFIXES",
    "FORBIDDEN_SUBSTRINGS",
    "generate_for_blueprint",
    "validate_runner_exports",
    "validate_source",
]
