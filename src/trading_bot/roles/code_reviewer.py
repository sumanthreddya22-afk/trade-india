"""Code Reviewer — Tier 5 lab role (Role 19).

For each pending TemplateProposal, runs three deterministic checks:
  1. AST allowlist (validate_ast)
  2. Sandbox runtime (run_in_sandbox) — pytest must pass within 30s
  3. (Optional, if creds available) LLM second-opinion call

On all-pass: writes the source to src/trading_bot/strategies/_evolved/<name>/
and updates review_status="accepted".
On any-fail: writes findings to _archive/ and updates review_status="rejected".
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.ast_validator import validate_ast
from trading_bot.roles.runner import BaseRole
from trading_bot.sandbox_runner import run_in_sandbox
from trading_bot.state_db import RoleRun, TemplateProposal


EVOLVED_DIR = Path("src/trading_bot/strategies/_evolved")
ARCHIVE_DIR = Path("src/trading_bot/strategies/_archive")


class CodeReviewerRole(BaseRole):
    name = "code_reviewer"
    tier = 5
    process = "lab"
    job_description = (
        "Reviews each pending TemplateProposal: AST allowlist, sandbox "
        "runtime test (30s walltime). Accepted templates land in _evolved/."
    )
    sla_seconds = 10 * 60
    upstream_roles = ["strategy_architect"]
    downstream_roles: list[str] = []

    def _do_work(self, ctx):
        with Session(self.engine) as session:
            pending = (
                session.query(TemplateProposal)
                .filter(TemplateProposal.review_status == "pending")
                .all()
            )
            ids = [p.id for p in pending]

        if not ids:
            return {"reviewed": 0, "accepted": 0, "rejected": 0}

        accepted_count = 0
        rejected_count = 0
        details: list[dict] = []

        for pid in ids:
            with Session(self.engine) as session:
                p = session.get(TemplateProposal, pid)
                if p is None or p.review_status != "pending":
                    continue
                name = p.name
                code = p.code
                tests = p.tests

            findings = self._review_one(name=name, code=code, tests=tests)
            verdict = "accepted" if findings["passes"] else "rejected"

            target_dir = EVOLVED_DIR if verdict == "accepted" else ARCHIVE_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            mod_dir = target_dir / name
            mod_dir.mkdir(exist_ok=True)
            (mod_dir / f"{name}.py").write_text(code)
            (mod_dir / f"test_{name}.py").write_text(tests)
            (mod_dir / "review.json").write_text(json.dumps(findings, indent=2))

            with Session(self.engine) as session:
                row = session.get(TemplateProposal, pid)
                row.review_status = verdict
                row.review_findings_json = json.dumps(findings)
                if verdict == "accepted":
                    row.accepted_at = dt.datetime.now(dt.timezone.utc)
                session.commit()

            details.append({"name": name, "verdict": verdict, "passes": findings["passes"]})
            if verdict == "accepted":
                accepted_count += 1
            else:
                rejected_count += 1

        return {
            "reviewed": len(ids),
            "accepted": accepted_count,
            "rejected": rejected_count,
            "details": details,
        }

    def _review_one(self, *, name: str, code: str, tests: str) -> dict:
        # Step 1: AST validation
        ast_report = validate_ast(code)
        ast_ok = ast_report.passes

        # Step 2: Sandbox runtime (skipped if AST fails — no point running unsound code)
        sandbox = None
        if ast_ok:
            sb = run_in_sandbox(
                module_name=name, source=code, test_source=tests, walltime_s=30
            )
            sandbox = {
                "passed": sb.passed,
                "exit_code": sb.exit_code,
                "walltime_s": sb.walltime_s,
                "timed_out": sb.timed_out,
                "stdout_tail": sb.stdout[-2000:],
                "stderr_tail": sb.stderr[-2000:],
            }

        passes = ast_ok and (sandbox is not None and sandbox["passed"])

        return {
            "passes": passes,
            "ast": {
                "passes": ast_report.passes,
                "forbidden_imports": ast_report.forbidden_imports,
                "forbidden_calls": ast_report.forbidden_calls,
                "syntax_error": ast_report.syntax_error,
            },
            "sandbox": sandbox,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            accepted = (
                session.query(TemplateProposal)
                .filter(
                    TemplateProposal.accepted_at.isnot(None),
                    TemplateProposal.accepted_at >= cutoff,
                )
                .count()
            )
            total = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        rate = accepted / total if total else 0.0
        return (
            "acceptance_rate",
            rate,
            f"{accepted} accepted across {total} reviewer runs in last {lookback_days}d",
        )
