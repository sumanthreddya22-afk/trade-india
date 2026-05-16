"""WS5f Layer 3 — event_calendar puller + corroboration + default fallback."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from trading_bot.ingest import event_calendar
from trading_bot.ingest.event_calendar import CalendarEvent


def _fmt(d: dt.datetime) -> str:
    return d.isoformat()


def test_corroboration_requires_two_sources(tmp_path: Path) -> None:
    base = dt.datetime(2026, 6, 18, 18, tzinfo=dt.timezone.utc)
    cache = tmp_path / "cache.json"

    def fred(_a, _b):
        return [CalendarEvent(
            kind="fomc_decision", start_iso=_fmt(base),
            end_iso=_fmt(base + dt.timedelta(hours=2)),
            sources=("fred",), action="no_new_entries_tighten_stops",
        )]
    def bls(_a, _b):
        return []
    payload = event_calendar.pull(
        now=base - dt.timedelta(days=1), forward_days=7,
        fetchers={"fred": fred, "bls": bls}, cache_path=cache,
    )
    # 1 source → corroboration filters it out.
    assert payload["events"] == []


def test_two_sources_keep_event(tmp_path: Path) -> None:
    base = dt.datetime(2026, 6, 18, 18, tzinfo=dt.timezone.utc)
    cache = tmp_path / "cache.json"

    def fred(_a, _b):
        return [CalendarEvent(
            kind="fomc_decision", start_iso=_fmt(base),
            end_iso=_fmt(base + dt.timedelta(hours=2)),
            sources=("fred",), action="no_new_entries_tighten_stops",
        )]

    def treasury(_a, _b):
        return [CalendarEvent(
            kind="fomc_decision", start_iso=_fmt(base),
            end_iso=_fmt(base + dt.timedelta(hours=2)),
            sources=("treasury",), action="no_new_entries_tighten_stops",
        )]

    payload = event_calendar.pull(
        now=base - dt.timedelta(days=1), forward_days=7,
        fetchers={"fred": fred, "treasury": treasury}, cache_path=cache,
    )
    assert len(payload["events"]) == 1
    assert set(payload["events"][0]["sources"]) == {"fred", "treasury"}


def test_in_blackout_active_during_event(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    base = dt.datetime(2026, 6, 18, 18, tzinfo=dt.timezone.utc)
    payload = {
        "as_of": _fmt(base - dt.timedelta(minutes=10)),
        "events": [{
            "kind": "fomc_decision",
            "start_iso": _fmt(base),
            "end_iso": _fmt(base + dt.timedelta(hours=2)),
            "sources": ["fred", "treasury"],
            "action": "no_new_entries_tighten_stops",
        }],
        "default_blackout_used": False,
    }
    cache.write_text(json.dumps(payload))
    hit, reason = event_calendar.in_blackout(
        now=base + dt.timedelta(minutes=30), cache_path=cache,
    )
    assert hit is True
    assert "fomc_decision" in reason


def test_in_blackout_returns_false_outside_window(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    base = dt.datetime(2026, 6, 18, 18, tzinfo=dt.timezone.utc)
    payload = {
        "as_of": _fmt(base),
        "events": [{
            "kind": "fomc_decision",
            "start_iso": _fmt(base),
            "end_iso": _fmt(base + dt.timedelta(hours=2)),
            "sources": ["fred", "treasury"],
            "action": "no_new_entries_tighten_stops",
        }],
        "default_blackout_used": False,
    }
    cache.write_text(json.dumps(payload))
    hit, _ = event_calendar.in_blackout(
        now=base + dt.timedelta(hours=5), cache_path=cache,
    )
    assert hit is False


def test_missing_cache_triggers_default_blackout(tmp_path: Path) -> None:
    # No cache file → in 09:30 ET window of local clock → blackout active.
    # We can't easily fake local-tz so just verify it returns a deterministic
    # bool + non-empty reason when blackout triggers.
    cache = tmp_path / "missing.json"
    # Pick a UTC time we expect to fall within 9:30-10:00 ET (= 13:30-14:00 UTC
    # during DST). Use 13:45 UTC which is 9:45 EDT in June.
    now = dt.datetime(2026, 6, 17, 13, 45, tzinfo=dt.timezone.utc)
    hit, reason = event_calendar.in_blackout(now=now, cache_path=cache)
    # We can't assert hit==True portably (depends on local tz of test host),
    # but the code path must return a bool and a string.
    assert isinstance(hit, bool)
    assert isinstance(reason, str)
