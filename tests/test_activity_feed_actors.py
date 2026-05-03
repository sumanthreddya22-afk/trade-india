"""Tests for the activity feed's persona-actor enrichment."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from trading_bot.dashboard.insights import (
    _event_to_role_name,
    build_activity_feed,
)


# ---------------------------------------------------------------------------
# _event_to_role_name
# ---------------------------------------------------------------------------


def test_event_to_role_name_strips_finish():
    assert _event_to_role_name("crypto_scan_finish") == "crypto_scan"


def test_event_to_role_name_strips_start():
    assert _event_to_role_name("intel_scan_start") == "intel_scan"


def test_event_to_role_name_strips_failed():
    assert _event_to_role_name("wheel_scan_failed") == "wheel_scan"


def test_event_to_role_name_handles_legacy_alias():
    """Legacy log lines say ``stock_scanner_finish`` — alias to ``intel_scan``."""
    assert _event_to_role_name("stock_scanner_finish") == "intel_scan"
    assert _event_to_role_name("crypto_scanner_finish") == "crypto_scan"


def test_event_to_role_name_returns_none_for_unknown():
    assert _event_to_role_name("daemon_boot") is None
    assert _event_to_role_name("") is None
    assert _event_to_role_name(None) is None


# ---------------------------------------------------------------------------
# build_activity_feed: actor enrichment
# ---------------------------------------------------------------------------


def _write_event(runs_root: Path, *, role: str, event: str, when: str = "12:00:00.000",
                 level: str = "info") -> None:
    today_utc = dt.datetime.now(dt.timezone.utc).date().isoformat()
    target = runs_root / today_utc / role
    target.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event, "level": level, "role": role,
    }
    (target / f"{when}.json").write_text(json.dumps(payload))


def test_finish_event_carries_persona_actors(tmp_path: Path):
    _write_event(tmp_path, role="daemon", event="crypto_scan_finish")
    feed = build_activity_feed(runs_dir=tmp_path, limit=10)
    assert len(feed) == 1
    line = feed[0]
    actor_names = [a["name"] for a in line.actors]
    assert "Sasha Volkov" in actor_names
    assert "Diane Pereira" in actor_names
    # Each entry has the keys the template needs
    for a in line.actors:
        assert "name" in a and "title" in a and "debate_role" in a and "pipeline" in a


def test_data_only_event_has_empty_actors(tmp_path: Path):
    _write_event(tmp_path, role="daemon", event="iv_capture_finish")
    feed = build_activity_feed(runs_dir=tmp_path, limit=10)
    assert len(feed) == 1
    assert feed[0].actors == []


def test_unmapped_event_has_empty_actors(tmp_path: Path):
    _write_event(tmp_path, role="daemon", event="daemon_boot")
    feed = build_activity_feed(runs_dir=tmp_path, limit=10)
    # daemon_boot does not match the _finish/_failed pattern → no actor lookup.
    assert len(feed) == 1
    assert feed[0].actors == []


def test_failed_event_still_carries_actors(tmp_path: Path):
    """When a debate-driven role fails, surface who was on the bot for that
    run so the operator knows who to look at."""
    _write_event(tmp_path, role="daemon", event="wheel_scan_failed", level="error")
    feed = build_activity_feed(runs_dir=tmp_path, limit=10)
    assert len(feed) == 1
    line = feed[0]
    assert line.level == "error"
    actor_names = {a["name"] for a in line.actors}
    assert "Aurelio Ortiz" in actor_names
    assert "Catherine Lloyd" in actor_names


def test_start_events_filtered_out(tmp_path: Path):
    """The feed builder suppresses _start events at info level — only _finish
    and _failed are kept. Verifies actors don't double-render."""
    _write_event(tmp_path, role="daemon",
                  event="crypto_scan_start", when="11:00:00.000")
    _write_event(tmp_path, role="daemon",
                  event="crypto_scan_finish", when="12:00:00.000")
    feed = build_activity_feed(runs_dir=tmp_path, limit=10)
    assert len(feed) == 1
    assert "Sasha Volkov" in [a["name"] for a in feed[0].actors]
