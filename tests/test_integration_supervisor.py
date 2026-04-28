"""Integration test: supervisor detects stale heartbeat and logs stall_detected.

Task 21 — Phase 1 plan.

Boots the supervisor subprocess with a pre-written heartbeat whose mtime
is 600 seconds in the past (well above the 5-minute threshold).  The
supervisor must:
  1. Detect the stale heartbeat.
  2. Write a stall_detected event under runs/<date>/supervisor/.

Note on email failures:
  The supervisor will attempt to send an alert email with bogus creds; this
  fails and is logged as alert_send_failed.  That is expected — the supervisor
  gracefully swallows transport errors (see supervisor.py _send_alert()).
  The test only asserts on the stall_detected event, not on email delivery.

Note on account check:
  The supervisor also tries an Alpaca account check using the bogus
  ALPACA_API_KEY=FAKE creds.  This fails with AlpacaClientError and is logged
  as account_check_failed.  Also expected and gracefully handled.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_VENV_PYTHON = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")


@pytest.mark.integration
def test_supervisor_logs_stall_when_heartbeat_old(tmp_path):
    """Supervisor must observe a stale heartbeat and log a stall_detected event."""
    # Pre-create an old heartbeat file.
    heartbeat_path = tmp_path / "heartbeat.json"
    heartbeat_path.write_text(json.dumps({
        "ts": "2020-01-01T00:00:00+00:00",
        "pid": 999,
        "version": "fake",
        "last_action": "boot",
    }))
    # Force mtime to 600 seconds ago so is_stale() returns True.
    old = time.time() - 600
    os.utime(heartbeat_path, (old, old))

    # Minimal config — watchdog_seconds=1 so supervisor loops fast.
    config_path = tmp_path / "paper_active.json"
    config_path.write_text(json.dumps({
        "version": "test",
        "active_template": "x",
        "params": {},
        "risk_caps": {
            "max_position_pct": 10,
            "daily_loss_pct": 3,
            "max_drawdown_pct": 20,
        },
        "cadence": {"watchdog_seconds": 1},
    }))

    pause_path = tmp_path / "pause.flag"
    runs_dir = tmp_path / "runs"
    state_db = tmp_path / "state.db"

    env = os.environ.copy()
    env.update({
        "TRADING_BOT_CONFIG": str(config_path),
        "TRADING_BOT_HEARTBEAT": str(heartbeat_path),
        "TRADING_BOT_PAUSE": str(pause_path),
        "TRADING_BOT_RUNS": str(runs_dir),
        "TRADING_BOT_STATE_DB": str(state_db),
        "TRADING_BOT_DAEMON_PLIST": "fake.label.that.does.not.exist",
        "TRADING_BOT_ALERT_TO": "test@local",  # SMTP will fail; that is expected
        # Provide dummy creds so Settings() can instantiate without .env
        "ALPACA_API_KEY": "FAKE",
        "ALPACA_API_SECRET": "FAKE",
        "GMAIL_USER": "fake@example.com",
        "GMAIL_APP_PASSWORD": "fake",
        "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
    })

    python = _VENV_PYTHON if Path(_VENV_PYTHON).exists() else sys.executable

    proc = subprocess.Popen(
        [python, "-m", "trading_bot.supervisor"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Poll until we see the stall_detected event or 10s pass.
        # We poll runs/<date>/supervisor/*.json rather than sleeping a fixed
        # amount, so the test remains fast when the supervisor is quick.
        deadline = time.time() + 10
        stall_events: list[dict] = []

        while time.time() < deadline and not stall_events:
            time.sleep(0.3)
            for f in runs_dir.glob("*/supervisor/*.json"):
                try:
                    ev = json.loads(f.read_text())
                    if ev.get("event") == "stall_detected":
                        stall_events.append(ev)
                except (json.JSONDecodeError, OSError):
                    pass

        assert stall_events, (
            "No stall_detected event found within 10s.\n"
            f"Run dir contents: {list(runs_dir.rglob('*'))}"
        )

        # Verify the event carries age information.
        ev = stall_events[0]
        assert "age_seconds" in ev, f"Missing age_seconds in event: {ev}"
        assert ev["age_seconds"] >= 300, (
            f"Stall age should be >= 300s, got {ev['age_seconds']}"
        )

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
