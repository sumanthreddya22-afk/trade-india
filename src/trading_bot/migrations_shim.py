"""Shared migration shim.

Runs ``alembic upgrade head`` idempotently. Safe to call on every CLI/daemon/
lab/supervisor entrypoint — when the schema is already at head, alembic
returns instantly with no DDL emitted.

Set ``TRADING_BOT_SKIP_MIGRATIONS=1`` to suppress (used by tests that set
their own schema via SQLAlchemy directly).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def ensure_migrations_at_head(*, log=None) -> bool:
    """Apply pending Alembic migrations. Idempotent.

    Returns ``True`` on success or skip; ``False`` on failure (caller decides
    whether to abort). Errors are logged via the optional ``log`` callable
    (signature ``log.event(name, **kwargs)`` / ``log.error(name, error=...)``)
    when supplied; otherwise stderr.
    """
    if os.environ.get("TRADING_BOT_SKIP_MIGRATIONS") == "1":
        if log is not None:
            log.event("alembic_upgrade_skipped", reason="TRADING_BOT_SKIP_MIGRATIONS=1")
        return True

    repo_root = Path(__file__).resolve().parent.parent.parent
    alembic_bin = repo_root / ".venv" / "bin" / "alembic"
    alembic_ini = repo_root / "migrations" / "alembic.ini"

    if not alembic_bin.exists():
        # Dev environments without a venv shouldn't crash — the daemon path
        # already requires the venv to exist, so this is just a safety net.
        if log is not None:
            log.event("alembic_upgrade_skipped", reason="venv_alembic_not_found")
        return True

    try:
        result = subprocess.run(
            [str(alembic_bin), "-c", str(alembic_ini), "upgrade", "head"],
            capture_output=True, text=True, timeout=30, cwd=str(repo_root),
        )
    except Exception as e:
        if log is not None:
            log.error("alembic_upgrade_exception", error=e)
        else:
            import sys
            print(f"alembic upgrade exception: {e}", file=sys.stderr)
        return False

    if result.returncode != 0:
        msg = RuntimeError(result.stderr or "alembic returned non-zero")
        if log is not None:
            log.error("alembic_upgrade_failed", error=msg)
        else:
            import sys
            print(f"alembic upgrade failed: {result.stderr}", file=sys.stderr)
        return False

    if log is not None:
        log.event("alembic_upgrade", result="ok")
    return True
