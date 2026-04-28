"""Lab subprocess smoke test — boot lab for ~3s, verify lab_boot event lands."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_VENV_PYTHON = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")


def _read_events(runs_dir: Path) -> list[dict]:
    events: list[dict] = []
    for d in runs_dir.glob("*/lab"):
        for f in d.glob("*.json"):
            try:
                events.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                pass
    return events


@pytest.mark.integration
def test_lab_cold_start_writes_boot_event(tmp_path):
    """Boot lab for ~3s, verify lab_boot event lands in runs/."""
    config_path = tmp_path / "paper_active.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "test-v1",
                "active_template": "momentum",
                "params": {},
                "fitness_at_promotion": None,
            }
        )
    )
    runs_dir = tmp_path / "runs"
    state_db = tmp_path / "state.db"

    env = os.environ.copy()
    env.update(
        {
            "TRADING_BOT_CONFIG": str(config_path),
            "TRADING_BOT_RUNS": str(runs_dir),
            "TRADING_BOT_STATE_DB": str(state_db),
            "ALPACA_API_KEY": "FAKE",
            "ALPACA_API_SECRET": "FAKE",
            "GMAIL_USER": "fake@example.com",
            "GMAIL_APP_PASSWORD": "fake",
            "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
            "TRADING_BOT_SKIP_MIGRATIONS": "1",
        }
    )

    python = _VENV_PYTHON if Path(_VENV_PYTHON).exists() else sys.executable

    proc = subprocess.Popen(
        [python, "-m", "trading_bot.lab"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 5
        boot_events: list[dict] = []
        while time.time() < deadline:
            time.sleep(0.3)
            events = _read_events(runs_dir)
            boot_events = [e for e in events if e.get("event") == "lab_boot"]
            if boot_events:
                break
        assert boot_events, (
            "No lab_boot event found.\n"
            f"stderr: {proc.stderr.read1(4096).decode(errors='replace') if proc.stderr else ''}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_lab_cli_subcommand_registered():
    """`bot lab` is a registered click subcommand."""
    from trading_bot.cli import main as cli_main

    cmd_names = list(cli_main.commands.keys())
    assert "lab" in cmd_names


def test_lab_register_jobs_creates_three_jobs():
    """Lab scheduler should have param_search + auto_promote + calibrate jobs."""
    from apscheduler.schedulers.background import BackgroundScheduler

    from trading_bot.lab import _register_lab_jobs

    sched = BackgroundScheduler()
    runners = {
        "param_search": lambda: None,
        "auto_promote": lambda: None,
        "calibrate": lambda: None,
    }
    _register_lab_jobs(sched, runners)
    job_ids = {j.id for j in sched.get_jobs()}
    assert job_ids == {"param_search", "auto_promote", "calibrate"}
