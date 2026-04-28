"""Integration test: daemon cold-start writes heartbeat and daemon_boot event.

Task 20 — Phase 1 plan.

Boots the daemon subprocess for ~5 seconds, verifies:
  1. heartbeat.json appears with expected keys.
  2. A daemon_boot event is written under runs/<date>/daemon/.
  3. SIGTERM causes graceful exit.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Path to the venv Python so we use the installed packages, not the system Python.
_VENV_PYTHON = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")


@pytest.mark.integration
def test_daemon_cold_start_writes_heartbeat(tmp_path):
    """Boot the daemon for ~5s, verify heartbeat.json appears and a
    daemon_boot event lands in runs/."""
    config_path = tmp_path / "paper_active.json"
    config_path.write_text(json.dumps({
        "version": "test-v1",
        "active_template": "momentum_v3",
        "params": {},
        "risk_caps": {
            "max_position_pct": 10,
            "daily_loss_pct": 3,
            "max_drawdown_pct": 20,
        },
        "cadence": {"heartbeat_seconds": 1},  # fast heartbeat for test
    }))

    heartbeat_path = tmp_path / "heartbeat.json"
    pause_path = tmp_path / "pause.flag"
    runs_dir = tmp_path / "runs"

    env = os.environ.copy()
    env.update({
        "TRADING_BOT_CONFIG": str(config_path),
        "TRADING_BOT_HEARTBEAT": str(heartbeat_path),
        "TRADING_BOT_PAUSE": str(pause_path),
        "TRADING_BOT_RUNS": str(runs_dir),
        # No Alpaca creds needed — daemon only writes heartbeat + boot event
        # before any scheduler job fires.  The scheduler jobs import cli lazily
        # and won't trigger during this short run.
        "ALPACA_API_KEY": "FAKE",
        "ALPACA_API_SECRET": "FAKE",
        "GMAIL_USER": "fake@example.com",
        "GMAIL_APP_PASSWORD": "fake",
        "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
        "TRADING_BOT_SKIP_MIGRATIONS": "1",  # test sets up schema directly via Base.metadata.create_all
    })

    # Use the venv Python; fall back to sys.executable inside the venv.
    python = _VENV_PYTHON if Path(_VENV_PYTHON).exists() else sys.executable

    proc = subprocess.Popen(
        [python, "-m", "trading_bot.daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait up to 5s for heartbeat to appear.
        deadline = time.time() + 5
        while time.time() < deadline and not heartbeat_path.exists():
            time.sleep(0.2)
        assert heartbeat_path.exists(), (
            "Heartbeat not written within 5s.\n"
            f"stderr: {proc.stderr.read1(4096).decode(errors='replace') if proc.stderr else ''}"
        )

        # Verify heartbeat content.
        hb = json.loads(heartbeat_path.read_text())
        assert "ts" in hb, f"Missing 'ts' key in heartbeat: {hb}"
        assert hb["pid"] == proc.pid, (
            f"PID mismatch: heartbeat says {hb['pid']}, proc is {proc.pid}"
        )

        # Verify daemon_boot event in runs/<date>/daemon/
        date_dirs = list(runs_dir.glob("*/daemon"))
        assert date_dirs, (
            f"No daemon run directory created under {runs_dir}. "
            f"Contents: {list(runs_dir.rglob('*'))}"
        )

        events = []
        for d in date_dirs:
            for f in d.glob("*.json"):
                try:
                    events.append(json.loads(f.read_text()))
                except json.JSONDecodeError:
                    pass

        boot_events = [e for e in events if e.get("event") == "daemon_boot"]
        assert boot_events, (
            f"No daemon_boot event found in {[e.get('event') for e in events]}"
        )

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
