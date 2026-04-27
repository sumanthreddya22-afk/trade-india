"""Heartbeat-based stall detection. Watchdog's job is to:
1. Periodically check heartbeat.json mtime.
2. If stale > max_age_seconds, attempt one launchctl kickstart of the daemon plist.
3. Caller (Supervisor) emits the email.
"""
from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import dataclass
from pathlib import Path

from trading_bot.state_heartbeat import is_stale


@dataclass
class StallVerdict:
    is_stalled: bool
    age_seconds: float


class StallDetector:
    def __init__(
        self,
        *,
        heartbeat_path: str | Path,
        max_age_seconds: int,
        plist_label: str | None = None,
    ):
        self.heartbeat_path = Path(heartbeat_path)
        self.max_age_seconds = max_age_seconds
        self.plist_label = plist_label

    def check(self) -> StallVerdict:
        p = self.heartbeat_path
        if not p.exists():
            return StallVerdict(is_stalled=True, age_seconds=float("inf"))
        age = dt.datetime.now().timestamp() - p.stat().st_mtime
        return StallVerdict(
            is_stalled=age > self.max_age_seconds,
            age_seconds=age,
        )

    def kickstart_daemon(self) -> bool:
        if self.plist_label is None:
            return False
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{_uid()}/{self.plist_label}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0


def _uid() -> int:
    import os
    return os.getuid()
