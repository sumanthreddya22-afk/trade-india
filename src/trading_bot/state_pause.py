"""Pause flag sentinel. If file exists, daemon must not place new orders.

Two complementary controls:

* ``pause.flag`` — global pause; checked by daemon._wrap to short-circuit any
  trade-placing lane. Used by AccountSentinel on drawdown breach.
* ``halted_strategies.txt`` — per-strategy pause; one strategy name per line
  (e.g. ``wheel``). Read into ``RiskState.halted_strategies`` so the operator
  can pause one lane (the wheel) while equity scans keep trading.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path


HALTED_STRATEGIES_PATH = Path(
    os.environ.get("TRADING_BOT_HALTED_STRATEGIES", "data/halted_strategies.txt")
)


def is_paused(path: str | Path) -> bool:
    return Path(path).exists()


def set_pause(path: str | Path, *, reason: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{reason}\n"
    p.write_text(payload)


def clear_pause(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def read_halted_strategies(path: str | Path) -> frozenset[str]:
    """Return the set of strategy names present in the file (one per line).

    Lines starting with ``#`` are treated as comments. Blank lines are ignored.
    Missing file or read error returns an empty set — fail-open so the
    operator can never accidentally lock the bot out by a transient FS issue.
    """
    p = Path(path)
    if not p.exists():
        return frozenset()
    try:
        raw = p.read_text()
    except OSError:
        return frozenset()
    names: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(stripped)
    return frozenset(names)


def set_halted_strategy(path: str | Path, name: str) -> None:
    """Append ``name`` to the halted-strategies file. Idempotent."""
    p = Path(path)
    current = set(read_halted_strategies(p))
    if name in current:
        return
    current.add(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(sorted(current)) + "\n")


def clear_halted_strategy(path: str | Path, name: str) -> None:
    """Remove ``name`` from the file. Removes the file if it becomes empty."""
    p = Path(path)
    current = set(read_halted_strategies(p))
    current.discard(name)
    if not current:
        p.unlink(missing_ok=True)
        return
    p.write_text("\n".join(sorted(current)) + "\n")
