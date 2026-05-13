"""Phase 6 — sandbox import guard."""
from __future__ import annotations

import pytest

from trading_bot.research import sandbox


def test_blocked_outside_sandbox_imports_freely() -> None:
    # Outside the sandbox, importing execution is fine.
    import trading_bot.execution                   # noqa: F401
    import trading_bot.kernel                       # noqa: F401


def test_sandbox_blocks_execution_import() -> None:
    with sandbox.activated():
        with pytest.raises(sandbox.SandboxImportError):
            __import__("trading_bot.execution")


def test_sandbox_blocks_kernel_import() -> None:
    with sandbox.activated():
        with pytest.raises(sandbox.SandboxImportError):
            __import__("trading_bot.kernel.boot")


def test_sandbox_restores_modules_on_exit() -> None:
    # Pre-import to populate the cache.
    import trading_bot.execution as _exe                # noqa: F401
    assert "trading_bot.execution" in __import__("sys").modules
    with sandbox.activated():
        with pytest.raises(sandbox.SandboxImportError):
            __import__("trading_bot.execution")
    # After exit, modules are restored.
    import sys
    assert "trading_bot.execution" in sys.modules


def test_sandbox_allows_ledger_and_research_imports() -> None:
    with sandbox.activated():
        # These must remain importable in the sandbox.
        __import__("trading_bot.ledger")
        __import__("trading_bot.research.mutation_engine")
        __import__("trading_bot.registry")
