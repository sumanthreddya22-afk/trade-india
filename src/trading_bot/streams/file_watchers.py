"""File-system watchers → event bus.

Some producers write directly to disk and don't import the bus
(structured-log JSON dropped into ``runs/<date>/<role>/``, the mailbox
routine writing ``data/llm_queue/done/<id>.json`` from a separate
process, scripts that overwrite ``data/last_scan.json`` without going
through ``trading_bot.last_scan.write_last_scan``). We catch those
writes here and emit equivalent bus events so the dashboard's
real-time pipeline stays accurate.

Design choice: no ``watchdog`` dependency. The set of paths is small
(<5 files + 2 directory trees), poll cadence is 1s, and a stat-call
per path is microseconds. Polling sidesteps the platform-specific
edge cases of inotify/FSEvents (rapid rename → modify → delete
sequences, NFS, etc) and keeps the deployment surface small.

Threading: one background thread, started/stopped with the daemon.
``bus.emit`` is non-blocking, so the loop never wedges.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trading_bot.event_bus import bus as bus_mod

logger = logging.getLogger(__name__)

# Polling cadence. Anything faster wastes CPU on stat() calls; anything
# slower starts to feel laggy in the dashboard. 1s is a sweet spot —
# the dashboard's existing 25s snapshot cache makes anything sub-second
# imperceptible to the user.
_POLL_S = 1.0


@dataclass
class _FileMtimeWatch:
    """Watch one file by mtime. On change, emit a single bus event with
    a small payload extracted from the file."""

    path: Path
    event_type: str
    source: str
    extract: Callable[[Path], dict[str, Any]] = field(default=lambda p: {})
    last_mtime: float = 0.0
    last_size: int = 0


@dataclass
class _DirNewfileWatch:
    """Watch a directory for new files (or new files under a depth-1
    subtree, e.g., ``runs/<date>/<role>/*.json``). On a new file, emit
    one event per new file. We track *names* not mtimes because
    contents can be rewritten in place (e.g., last_scan.json) — those
    are handled by ``_FileMtimeWatch``.
    """

    root: Path
    glob: str
    event_type: str
    source: str
    extract: Callable[[Path], dict[str, Any]] = field(default=lambda p: {})
    seen: set[str] = field(default_factory=set)
    initialized: bool = False


class FileWatcherRunner:
    """One thread, many watches.

    Initial pass on start populates the ``seen`` sets for directory
    watches so we don't fire a flood of "newly seen" events for files
    that already existed before the daemon booted.
    """

    def __init__(self, *, poll_interval: float = _POLL_S) -> None:
        self._poll = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watches: list[_FileMtimeWatch | _DirNewfileWatch] = []

    def watch_file(self, w: _FileMtimeWatch) -> None:
        self._watches.append(w)

    def watch_dir(self, w: _DirNewfileWatch) -> None:
        self._watches.append(w)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._watches:
            logger.info("file_watchers: no watches configured; skipping")
            return
        self._stop.clear()
        # Prime initial state so we don't fire "everything is new" on boot.
        for w in self._watches:
            try:
                if isinstance(w, _FileMtimeWatch):
                    if w.path.exists():
                        st = w.path.stat()
                        w.last_mtime = st.st_mtime
                        w.last_size = st.st_size
                else:  # dir
                    w.seen = {p.name for p in w.root.glob(w.glob)} if w.root.exists() else set()
                    w.initialized = True
            except Exception:
                logger.exception("file_watchers: failed to prime %r", w)
        t = threading.Thread(target=self._run, name="file-watchers", daemon=True)
        t.start()
        self._thread = t
        bus_mod.emit("process.started",
                     {"component": "file_watchers", "watch_count": len(self._watches)},
                     source="file_watchers")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                for w in self._watches:
                    if isinstance(w, _FileMtimeWatch):
                        self._tick_file(w)
                    else:
                        self._tick_dir(w)
            except Exception:
                logger.exception("file_watchers: tick error (continuing)")

    @staticmethod
    def _tick_file(w: _FileMtimeWatch) -> None:
        try:
            if not w.path.exists():
                return
            st = w.path.stat()
        except FileNotFoundError:
            return
        # Trigger on either mtime or size change. mtime alone misses
        # in-place rewrites that finish in the same second.
        if st.st_mtime == w.last_mtime and st.st_size == w.last_size:
            return
        w.last_mtime = st.st_mtime
        w.last_size = st.st_size
        try:
            payload = w.extract(w.path) or {}
        except Exception:
            payload = {}
        bus_mod.emit(w.event_type, payload, source=w.source)

    @staticmethod
    def _tick_dir(w: _DirNewfileWatch) -> None:
        if not w.root.exists():
            return
        try:
            current = {p for p in w.root.glob(w.glob) if p.is_file()}
        except Exception:
            return
        names = {p.name for p in current}
        new_names = names - w.seen
        if not new_names:
            w.seen = names  # also handle deletions cleanly
            return
        for name in sorted(new_names):
            full = w.root / name if (w.root / name).exists() else next(
                (p for p in current if p.name == name), None,
            )
            if full is None:
                continue
            try:
                payload = w.extract(full) or {}
            except Exception:
                payload = {}
            bus_mod.emit(w.event_type, payload, source=w.source)
        w.seen = names


# ---------------------------------------------------------------------------
# Extractors — pull a small, dashboard-useful payload out of each file.
# ---------------------------------------------------------------------------
def _extract_last_scan(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
        decisions = raw.get("decisions") or []
        actions: dict[str, int] = {}
        for d in decisions:
            a = d.get("action", "?")
            actions[a] = actions.get(a, 0) + 1
        return {
            "command": raw.get("command"),
            "regime": raw.get("regime"),
            "universe_size": raw.get("universe_size"),
            "n_decisions": len(decisions),
            "actions": actions,
            "watcher": "file",
        }
    except Exception:
        return {"watcher": "file"}


def _extract_opportunities(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
        return {"size": st.st_size, "mtime": st.st_mtime, "watcher": "file"}
    except Exception:
        return {"watcher": "file"}


def _extract_scout(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
        cands = raw.get("candidates") or raw if isinstance(raw, list) else []
        return {"n_candidates": len(cands), "watcher": "file"}
    except Exception:
        return {"watcher": "file"}


def _extract_mailbox_done(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
        return {
            "brief_id": raw.get("id") or path.stem,
            "model_used": raw.get("model_used"),
            "has_error": bool(raw.get("error")),
            "watcher": "file",
        }
    except Exception:
        return {"brief_id": path.stem, "watcher": "file"}


def _extract_runs(path: Path) -> dict[str, Any]:
    # path = runs/<UTC date>/<role>/<file>.json — derive role from parent.
    role = path.parent.name if path.parent else ""
    return {"role": role, "filename": path.name, "watcher": "file"}


# ---------------------------------------------------------------------------
# Daemon integration helper
# ---------------------------------------------------------------------------
def maybe_start(*,
                last_scan: str = "data/last_scan.json",
                opportunities_md: str = "strategy/opportunities.md",
                scout_json: str = "data/wheel_scout_candidates.json",
                llm_done_dir: str = "data/llm_queue/done",
                runs_dir: str = "runs") -> FileWatcherRunner | None:
    """Construct + start the watcher. Returns the runner so the daemon
    can stop it on shutdown. ``TRADING_BOT_FILE_WATCHERS_DISABLED=1``
    skips startup."""
    if os.environ.get("TRADING_BOT_FILE_WATCHERS_DISABLED") == "1":
        logger.info("file_watchers: disabled via env")
        return None
    runner = FileWatcherRunner()
    runner.watch_file(_FileMtimeWatch(
        path=Path(last_scan), event_type="scan.completed",
        source="file_watcher.last_scan", extract=_extract_last_scan,
    ))
    runner.watch_file(_FileMtimeWatch(
        path=Path(opportunities_md), event_type="opportunities.updated",
        source="file_watcher.opportunities", extract=_extract_opportunities,
    ))
    runner.watch_file(_FileMtimeWatch(
        path=Path(scout_json), event_type="scout.completed",
        source="file_watcher.scout", extract=_extract_scout,
    ))
    # Mailbox done dir: every new file in done/ is a completed brief.
    # The mailbox process emits directly when it imports the bus, but
    # this is a defensive backup in case the routine runs in an
    # environment that can't import trading_bot.event_bus.
    runner.watch_dir(_DirNewfileWatch(
        root=Path(llm_done_dir), glob="*.json",
        event_type="mailbox.brief.completed",
        source="file_watcher.mailbox", extract=_extract_mailbox_done,
    ))
    # runs/<UTC date>/<role>/*.json — depth-2 glob so we catch any role.
    runner.watch_dir(_DirNewfileWatch(
        root=Path(runs_dir), glob="*/*/*.json",
        event_type="activity.appended",
        source="file_watcher.runs", extract=_extract_runs,
    ))
    runner.start()
    return runner
