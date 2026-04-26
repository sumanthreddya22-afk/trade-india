"""Persist the most recent scan's decisions so the dashboard can show
'what did the bot consider on its last fire and why.'

Every scan command (intel-scan, crypto-scan, full-run, eod-report) writes
this file at the end of its run. Append-style would balloon over time;
overwrite-on-each-run keeps it cheap. If long-term history is wanted
later, add a SQLite-backed store instead — this file is intentionally
the simplest possible thing that works for the dashboard.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.orchestrator import Decision, ScanResult

LAST_SCAN_PATH = Path("data/last_scan.json")


@dataclass(frozen=True)
class PersistedDecision:
    symbol: str
    action: str
    reason: str


@dataclass(frozen=True)
class PersistedScan:
    command: str
    regime: str
    universe_size: int
    timestamp: datetime
    decisions: list[PersistedDecision]


def write_last_scan(
    *, command: str, regime: str, universe_size: int, result: ScanResult,
    path: Path = LAST_SCAN_PATH,
) -> None:
    """Overwrite-on-write — never raises (best-effort)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "command": command,
            "regime": regime,
            "universe_size": universe_size,
            "timestamp": result.timestamp.isoformat(),
            "decisions": [
                {"symbol": d.symbol, "action": d.action, "reason": d.reason}
                for d in result.decisions
            ],
        }
        path.write_text(json.dumps(payload, indent=2, default=str))
    except Exception:
        # Best-effort — never block a scan because we couldn't write its log.
        pass


def read_last_scan(path: Path = LAST_SCAN_PATH) -> PersistedScan | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return PersistedScan(
            command=raw.get("command", "?"),
            regime=raw.get("regime", "?"),
            universe_size=int(raw.get("universe_size", 0)),
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            decisions=[
                PersistedDecision(
                    symbol=d.get("symbol", "?"),
                    action=d.get("action", "?"),
                    reason=d.get("reason", ""),
                )
                for d in raw.get("decisions", [])
            ],
        )
    except Exception:
        return None
