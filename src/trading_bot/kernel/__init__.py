"""L5 deterministic trading kernel.

Plan v4 §3. Phase 2 ships boot-time integrity checks (``kernel.boot``).
The kernel runner itself lands in Phase 5+.
"""
from trading_bot.kernel.boot import BootReport, run_boot_checks  # noqa: F401

__all__ = ["BootReport", "run_boot_checks"]
