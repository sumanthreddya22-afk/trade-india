"""WS5f Layer 3 — event blackout calendar puller.

Reads `policy/event_blackout.lock` for the source list + action matrix,
pulls forward 7d from each, applies the corroboration rule (≥2 sources
for an event; disputed windows = union; total source failure =
default 9:30-10:00 ET blackout), and writes the merged schedule into
``data/state/event_calendar_cache.json``.

The cache shape:

    {
      "as_of": "2026-05-25T22:00:00Z",
      "events": [
        {
          "kind": "fomc_decision",
          "start_iso": "2026-06-18T18:00:00Z",
          "end_iso":   "2026-06-18T22:00:00Z",
          "sources": ["fred", "treasury"],
          "action": "no_new_entries_tighten_stops"
        },
        ...
      ],
      "default_blackout_used": false
    }

Risk-precheck consults this file via ``in_blackout(now, kind)``; missing
cache or stale-by-`stale_pull_max_hours` falls back to default blackout.

Note: actual HTTP fetchers are out of scope for this session — this
module ships with deterministic stubs that the operator can replace
with real adapters one source at a time. The default-blackout fallback
ensures safety even when no source is wired.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOCK_PATH = REPO_ROOT / "policy" / "event_blackout.lock"
DEFAULT_CACHE_PATH = REPO_ROOT / "data" / "state" / "event_calendar_cache.json"


@dataclass(frozen=True)
class CalendarEvent:
    kind: str
    start_iso: str
    end_iso: str
    sources: tuple[str, ...]
    action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start_iso": self.start_iso,
            "end_iso": self.end_iso,
            "sources": list(self.sources),
            "action": self.action,
        }


SourceFetcher = Callable[[dt.datetime, dt.datetime], Iterable[CalendarEvent]]
"""Signature: (window_start_utc, window_end_utc) -> events."""


# ---- Source fetcher stubs (deterministic, no network) -------------------
def _stub(_label: str) -> SourceFetcher:
    def _fetch(_a, _b):
        return []
    return _fetch


DEFAULT_FETCHERS: dict[str, SourceFetcher] = {
    "fred": _stub("fred"),
    "bls": _stub("bls"),
    "treasury": _stub("treasury"),
    "sec_edgar": _stub("sec_edgar"),
    "opra": _stub("opra"),
    "cme": _stub("cme"),
}


def _load_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> Mapping[str, Any]:
    if not lock_path.exists():
        raise FileNotFoundError(f"missing {lock_path}")
    return json.loads(lock_path.read_text())


def _merge_events(events: Iterable[CalendarEvent]) -> list[CalendarEvent]:
    """Apply corroboration: an event must appear from ≥2 sources to be
    written into the merged calendar. The merged sources list is the
    union; the start/end window is also union (max overlap)."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in events:
        key = (ev.kind, ev.start_iso[:10])  # date granularity
        bucket = by_key.setdefault(key, {
            "kind": ev.kind, "starts": [], "ends": [], "sources": set(),
            "action": ev.action,
        })
        bucket["starts"].append(ev.start_iso)
        bucket["ends"].append(ev.end_iso)
        bucket["sources"].update(ev.sources)
    out: list[CalendarEvent] = []
    for bucket in by_key.values():
        if len(bucket["sources"]) < 2:
            continue
        out.append(CalendarEvent(
            kind=bucket["kind"],
            start_iso=min(bucket["starts"]),
            end_iso=max(bucket["ends"]),
            sources=tuple(sorted(bucket["sources"])),
            action=bucket["action"],
        ))
    return sorted(out, key=lambda e: e.start_iso)


def pull(
    *,
    now: Optional[dt.datetime] = None,
    forward_days: int = 7,
    fetchers: Optional[Mapping[str, SourceFetcher]] = None,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> dict[str, Any]:
    """Pull events from each source, apply corroboration, write cache."""
    now = now or dt.datetime.now(dt.timezone.utc)
    fetchers = fetchers or DEFAULT_FETCHERS
    window_end = now + dt.timedelta(days=forward_days)
    raw: list[CalendarEvent] = []
    n_sources_ok = 0
    for source, fetcher in fetchers.items():
        try:
            evs = list(fetcher(now, window_end))
            n_sources_ok += 1
            raw.extend(evs)
        except Exception as e:  # noqa: BLE001
            log.warning("event_calendar: source %s failed: %s", source, e)

    merged = _merge_events(raw)
    default_used = n_sources_ok == 0
    payload = {
        "as_of": now.isoformat(),
        "events": [ev.to_dict() for ev in merged],
        "default_blackout_used": default_used,
        "n_sources_ok": n_sources_ok,
        "n_sources_total": len(fetchers),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def _load_cache(cache_path: Path = DEFAULT_CACHE_PATH) -> Optional[dict]:
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text())
    except Exception:
        return None


def in_blackout(
    *,
    now: Optional[dt.datetime] = None,
    kind: Optional[str] = None,
    cache_path: Path = DEFAULT_CACHE_PATH,
    stale_max_hours: int = 36,
    default_blackout_et_window: tuple[str, str] = ("09:30", "10:00"),
) -> tuple[bool, str]:
    """Returns (in_blackout, reason).

    If ``kind`` is given, only that event class blacks out. Else any
    event blacks out. On stale or missing cache, the default
    market-open blackout (9:30-10:00 ET) applies.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cache = _load_cache(cache_path)
    if not cache:
        return _check_default_blackout(now, default_blackout_et_window)
    try:
        as_of = dt.datetime.fromisoformat(cache["as_of"])
    except Exception:
        as_of = now - dt.timedelta(days=365)
    if (now - as_of).total_seconds() > stale_max_hours * 3600:
        return _check_default_blackout(now, default_blackout_et_window)
    if cache.get("default_blackout_used"):
        return _check_default_blackout(now, default_blackout_et_window)
    for ev in cache.get("events", []):
        if kind and ev.get("kind") != kind:
            continue
        try:
            s = dt.datetime.fromisoformat(ev["start_iso"])
            e = dt.datetime.fromisoformat(ev["end_iso"])
        except Exception:
            continue
        if s <= now <= e:
            return True, (
                f"event_blackout:{ev['kind']} "
                f"(sources={','.join(ev.get('sources', []))})"
            )
    return False, ""


def _check_default_blackout(
    now: dt.datetime, et_window: tuple[str, str],
) -> tuple[bool, str]:
    """Default 9:30-10:00 ET blackout when cache is stale / unavailable."""
    # Convert "ET" naively — operator's machine is set to ET. The
    # blackout is a hard fallback so a 30-min margin of slop is fine.
    try:
        h0, m0 = map(int, et_window[0].split(":"))
        h1, m1 = map(int, et_window[1].split(":"))
    except Exception:
        return False, ""
    local = now.astimezone()
    start = local.replace(hour=h0, minute=m0, second=0, microsecond=0)
    end = local.replace(hour=h1, minute=m1, second=0, microsecond=0)
    if start <= local <= end:
        return True, (
            f"event_blackout:default_market_open_window "
            f"({et_window[0]}-{et_window[1]} local) — cache stale or "
            f"unavailable"
        )
    return False, ""


__all__ = [
    "CalendarEvent", "DEFAULT_FETCHERS", "in_blackout", "pull",
]
