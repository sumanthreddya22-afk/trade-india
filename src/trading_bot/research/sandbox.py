"""Mutation-sandbox import guard.

Plan v4 §14 P1: "Mutation sandbox isolation — Mutation runner cannot
access broker creds or place orders; attempting to import execution
module raises ImportError."

The guard installs a ``sys.meta_path`` finder that intercepts imports
of the blocked module list and raises ``ImportError``. Activated as a
context manager so cleanup is guaranteed:

    with sandbox.activated():
        run_mutation_cycle(...)
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from contextlib import contextmanager
from dataclasses import dataclass

BLOCKED_PREFIXES: tuple[str, ...] = (
    "trading_bot.execution",
    "trading_bot.kernel",
    "trading_bot.risk.precheck",
    "trading_bot.risk.order_router",     # safety: not a real module today but if it exists later
    "trading_bot.shared.alpaca_client",
)


class SandboxImportError(ImportError):
    """Distinct subclass so callers can catch sandbox-specific failures."""


class _BlockingFinder(importlib.abc.MetaPathFinder):
    """sys.meta_path hook that raises on blocked imports."""

    def find_spec(self, fullname, path=None, target=None):
        for prefix in BLOCKED_PREFIXES:
            if fullname == prefix or fullname.startswith(prefix + "."):
                raise SandboxImportError(
                    f"sandbox: import of {fullname!r} is forbidden "
                    f"in the L3 research factory (Plan v4 §14 P1)"
                )
        return None  # let the rest of meta_path handle it


@dataclass
class SandboxState:
    finder: _BlockingFinder
    pre_blocked_modules: dict[str, object]
    """Modules already imported when we activated; we evict them so
    re-imports during the sandbox raise rather than hit the cache."""


@contextmanager
def activated():
    """Activate sandbox imports for the duration of the with-block.

    Already-imported blocked modules are temporarily removed from
    ``sys.modules`` (and restored on exit) so any code path attempting
    to use them inside the sandbox re-imports and trips the finder.
    """
    finder = _BlockingFinder()
    saved: dict[str, object] = {}
    for name in list(sys.modules.keys()):
        for prefix in BLOCKED_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                saved[name] = sys.modules[name]
                del sys.modules[name]
                break
    sys.meta_path.insert(0, finder)
    try:
        yield SandboxState(finder=finder, pre_blocked_modules=saved)
    finally:
        try:
            sys.meta_path.remove(finder)
        except ValueError:
            pass
        sys.modules.update(saved)


__all__ = [
    "BLOCKED_PREFIXES",
    "SandboxImportError",
    "SandboxState",
    "activated",
]
