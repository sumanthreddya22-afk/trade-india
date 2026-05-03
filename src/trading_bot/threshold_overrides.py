"""threshold_overrides — read/write helpers for adaptive threshold knobs.

The nightly ``threshold_tuner`` role writes rows to the
``threshold_overrides`` table. Hot-path code (risk_manager, wheel_lane,
chain.py, orchestrator) calls ``lookup()`` to fetch the current value
for a knob, falling back silently to the static YAML config when no
fresh override exists. This is the same freshness-gate shape as the
wheel scout JSON: a missed nightly run does NOT fail the trading
loop — it just means we run on yesterday's static defaults.

Defense-in-depth: even though the writer is supposed to clamp values
to ``[bounds_min, bounds_max]`` before insert, ``lookup()`` re-clamps
on read. A buggy writer or a hand-edited row cannot escape the safe
range.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from trading_bot.state_db import ThresholdOverride


DEFAULT_MAX_AGE_HOURS = 36


def lookup(
    engine,
    *,
    knob: str,
    regime: str | None = None,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    now: dt.datetime | None = None,
) -> float | None:
    """Return the current override value for ``knob`` or ``None``.

    Resolution order:
      1. Most-recent un-expired row matching (knob, regime).
      2. Most-recent un-expired row matching (knob, regime IS NULL).
      3. None — caller falls back to static config.

    Stale rows (older than ``max_age_hours``) are ignored even if not
    explicitly expired. This protects against a stuck tuner: if the
    nightly job hasn't run for 2+ days, we want the static config to
    take over rather than a value computed from very-old data.
    """
    if not knob:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=max_age_hours)

    with Session(engine) as session:
        # Phase E — live reads must skip shadow rows. Shadow rows are
        # what-if values the tuner is evaluating; only the analyzer reads
        # them (via list_shadow).
        rows = (
            session.query(ThresholdOverride)
            .filter(ThresholdOverride.knob == knob)
            .filter(ThresholdOverride.set_at >= cutoff)
            .filter(
                (ThresholdOverride.shadow.is_(False))
                | (ThresholdOverride.shadow.is_(None))
            )
            .order_by(desc(ThresholdOverride.set_at))
            .all()
        )

    if not rows:
        return None

    candidates = [_resolve(r, regime, now) for r in rows]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None
    # Prefer regime-specific match; fall back to regime-agnostic.
    regime_match = next((c for c in candidates if c[0] == regime), None)
    fallback = next((c for c in candidates if c[0] is None), None)
    chosen = regime_match or fallback
    if chosen is None:
        return None
    _, value, lo, hi = chosen
    return _clamp(value, lo, hi)


def _resolve(
    row: ThresholdOverride, requested_regime: str | None, now: dt.datetime
) -> tuple[str | None, float, float, float] | None:
    """Return (regime, value, lo, hi) if the row is alive and matches the
    requested regime (either exactly, or it's a regime-agnostic row).
    """
    expires_at = row.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
        if expires_at <= now:
            return None
    if row.regime is not None and row.regime != requested_regime:
        # Regime-specific row that doesn't match; not for us.
        return None
    return (row.regime, float(row.value), float(row.bounds_min), float(row.bounds_max))


def _clamp(value: float, lo: float, hi: float) -> float:
    if lo > hi:
        # malformed bounds; act as if missing
        return value
    return max(lo, min(hi, value))


def write_override(
    engine,
    *,
    knob: str,
    value: float,
    bounds_min: float,
    bounds_max: float,
    regime: str | None = None,
    set_by: str = "threshold_tuner",
    signal_summary: dict[str, Any] | None = None,
    expires_at: dt.datetime | None = None,
    shadow: bool = False,
    now: dt.datetime | None = None,
) -> ThresholdOverride:
    """Insert a new override row. Value is clamped to bounds before write.

    Append-only: never updates existing rows. The reader picks the most
    recent matching row, so writing again with the same (knob, regime)
    naturally supersedes the prior value.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    clamped = _clamp(float(value), float(bounds_min), float(bounds_max))
    payload = json.dumps(signal_summary or {}, sort_keys=True, default=str)
    row = ThresholdOverride(
        knob=knob,
        value=clamped,
        regime=regime,
        bounds_min=float(bounds_min),
        bounds_max=float(bounds_max),
        set_at=now,
        set_by=set_by,
        signal_summary=payload,
        expires_at=expires_at,
        shadow=bool(shadow),
    )
    with Session(engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    # Real-time bus emit (Phase 2). One event per override write so the
    # _threshold_overrides dashboard fragment can refresh on push.
    try:
        from trading_bot.event_bus import bus as _bus
        _bus.emit(
            "threshold.updated",
            {
                "knob": knob, "value": clamped, "regime": regime,
                "bounds_min": float(bounds_min), "bounds_max": float(bounds_max),
                "set_by": set_by,
            },
            source="threshold_overrides",
        )
    except Exception:
        pass
    return row


def list_active(
    engine,
    *,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    now: dt.datetime | None = None,
) -> list[ThresholdOverride]:
    """Return one row per (knob, regime) — the most recent active value.

    Used by the dashboard tile and the operator inspect tool. Stale and
    expired rows are excluded.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=max_age_hours)
    with Session(engine) as session:
        rows = (
            session.query(ThresholdOverride)
            .filter(ThresholdOverride.set_at >= cutoff)
            .order_by(desc(ThresholdOverride.set_at))
            .all()
        )
    seen: set[tuple[str, str | None]] = set()
    out: list[ThresholdOverride] = []
    for r in rows:
        key = (r.knob, r.regime)
        if key in seen:
            continue
        if r.expires_at is not None:
            ex = r.expires_at
            if ex.tzinfo is None:
                ex = ex.replace(tzinfo=dt.timezone.utc)
            if ex <= now:
                continue
        seen.add(key)
        out.append(r)
    return out
