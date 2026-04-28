"""Sandboxed runtime check for Architect-proposed strategy modules.

Forks a subprocess to run pytest against (source, test_source) pair under
walltime + memory limits. Returns a SandboxResult with stdout, exit code,
and elapsed walltime.

NOTE: macOS ignores RLIMIT_AS, so we rely on subprocess walltime + RLIMIT_CPU
as the practical guard. Strategy code is allowlisted by the AST validator
upstream; this is a defense-in-depth runtime check.
"""
from __future__ import annotations

import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    walltime_s: float
    timed_out: bool


def _set_rlimits(cpu_seconds: int, mem_mb: int) -> None:
    """Called in the child process to apply rlimits."""
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
    except (ValueError, OSError):
        # macOS doesn't honor RLIMIT_AS; that's OK
        pass


def run_in_sandbox(
    *,
    module_name: str,
    source: str,
    test_source: str,
    walltime_s: int = 30,
    mem_mb: int = 512,
) -> SandboxResult:
    """Write source + test to tempdir, run pytest in subprocess, return result.

    Returns passed=True iff exit_code==0 within walltime_s.
    """
    with tempfile.TemporaryDirectory(prefix="lab_sandbox_") as tmpdir:
        td = Path(tmpdir)
        (td / f"{module_name}.py").write_text(source)
        (td / f"test_{module_name}.py").write_text(test_source)

        cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(td / f"test_{module_name}.py"),
            "-x",
            "--tb=short",
            "-q",
        ]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=walltime_s,
                preexec_fn=lambda: _set_rlimits(walltime_s, mem_mb),
                cwd=str(td),
            )
            elapsed = time.monotonic() - start
            return SandboxResult(
                passed=(proc.returncode == 0),
                exit_code=proc.returncode,
                stdout=proc.stdout.decode("utf-8", errors="replace"),
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                walltime_s=elapsed,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return SandboxResult(
                passed=False,
                exit_code=-1,
                stdout="",
                stderr=f"sandbox timed out after {walltime_s}s",
                walltime_s=elapsed,
                timed_out=True,
            )
