"""Crypto adaptive thresholds (Phase 1E).

Read + write API for ``threshold_overrides_crypto``. Pure functions where
possible (lookup by knob/regime); persistence functions for the tuner.

Knobs the lesson loop / threshold tuner can tune over time:
  - ``intel_threshold``           — minimum candidate score for scout-debate
                                     consideration. Per-regime: e.g. 3.0 in
                                     trending_up, 4.5 in range, 7.0 in
                                     trending_down.
  - ``source_weight:<source>``    — per-source weight override (e.g.
                                     ``source_weight:whale_alert``)
  - ``hold_score_drop_threshold`` — fraction the score must drop to fire
                                     a hold-debate. Volatility-aware.
  - ``regime_tp_ratio``           — overrides the static REGIME_TP_RATIO map

Default fallbacks come from ``strategy/config.yaml`` or
``adversarial.CryptoAdversarialThresholds``-style dataclasses inside
each module. ``lookup_threshold`` returns ``None`` when no override
exists; the caller plugs in the default.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import ThresholdOverrideCrypto

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read API — called by the per-debate / per-roll-up code at decision time
# ---------------------------------------------------------------------------


def lookup_threshold(
    engine: Any,
    *,
    knob: str,
    regime: Optional[str] = None,
    include_shadow: bool = False,
    now: Optional[dt.datetime] = None,
) -> Optional[float]:
    """Return the most recent live (non-shadow, non-superseded) threshold
    override for a (knob, regime) pair.

    Falls through:
      1. (knob, regime, live, not superseded) — most specific
      2. (knob, regime IS NULL, live, not superseded) — global fallback
      3. None — caller plugs in code default

    ``include_shadow=True`` lets analyzers compare shadow vs. live.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        # Step 1: regime-specific live override
        if regime is not None:
            row = (
                session.query(ThresholdOverrideCrypto)
                .filter(ThresholdOverrideCrypto.knob == knob)
                .filter(ThresholdOverrideCrypto.regime == regime)
                .filter(ThresholdOverrideCrypto.superseded_at.is_(None))
                .filter(_shadow_filter(include_shadow))
                .order_by(ThresholdOverrideCrypto.proposed_at.desc())
                .first()
            )
            if row is not None:
                return float(row.proposed_value)

        # Step 2: global (regime IS NULL)
        row = (
            session.query(ThresholdOverrideCrypto)
            .filter(ThresholdOverrideCrypto.knob == knob)
            .filter(ThresholdOverrideCrypto.regime.is_(None))
            .filter(ThresholdOverrideCrypto.superseded_at.is_(None))
            .filter(_shadow_filter(include_shadow))
            .order_by(ThresholdOverrideCrypto.proposed_at.desc())
            .first()
        )
        if row is not None:
            return float(row.proposed_value)

    return None


def _shadow_filter(include_shadow: bool):
    if include_shadow:
        # Tautology: matches all rows
        return ThresholdOverrideCrypto.id == ThresholdOverrideCrypto.id
    return ThresholdOverrideCrypto.shadow.is_(False)


def lookup_source_weight(
    engine: Any,
    source: str,
    *,
    fallback: float,
    now: Optional[dt.datetime] = None,
) -> float:
    """Convenience wrapper for ``source_weight:<source>`` knob lookup.

    Returns the override value if one exists, else ``fallback`` (typically
    the static value from ``CRYPTO_SOURCE_WEIGHTS``).
    """
    val = lookup_threshold(
        engine, knob=f"source_weight:{source}", regime=None, now=now,
    )
    return float(val) if val is not None else float(fallback)


# ---------------------------------------------------------------------------
# Write API — called by the threshold tuner (Phase 1E follow-on)
# ---------------------------------------------------------------------------


def write_proposal(
    engine: Any,
    *,
    knob: str,
    proposed_value: float,
    regime: Optional[str] = None,
    rationale: str = "",
    proposed_by: str = "threshold_tuner",
    shadow: bool = True,
    now: Optional[dt.datetime] = None,
) -> int:
    """Persist a tuner proposal. Defaults to shadow=True (14d shadow rollout)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        row = ThresholdOverrideCrypto(
            knob=knob,
            regime=regime,
            proposed_value=proposed_value,
            rationale=rationale,
            proposed_by=proposed_by,
            proposed_at=now,
            shadow=shadow,
            shadow_what_if_pnl=None,
            promoted_to_live_at=None,
            superseded_at=None,
        )
        session.add(row)
        session.commit()
        return row.id


def promote_to_live(
    engine: Any,
    *,
    override_id: int,
    now: Optional[dt.datetime] = None,
) -> bool:
    """Flip a shadow override to live. Operator-driven (manual review).

    Also marks any prior live overrides for the same (knob, regime) as
    superseded so ``lookup_threshold`` returns the new one.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        row = session.get(ThresholdOverrideCrypto, override_id)
        if row is None:
            return False
        if not row.shadow:
            # Already live — no-op
            return True

        # Supersede any prior live row for this (knob, regime)
        prior = (
            session.query(ThresholdOverrideCrypto)
            .filter(ThresholdOverrideCrypto.id != override_id)
            .filter(ThresholdOverrideCrypto.knob == row.knob)
            .filter(_regime_match(row.regime))
            .filter(ThresholdOverrideCrypto.shadow.is_(False))
            .filter(ThresholdOverrideCrypto.superseded_at.is_(None))
            .all()
        )
        for p in prior:
            p.superseded_at = now

        row.shadow = False
        row.promoted_to_live_at = now
        session.commit()
        return True


def _regime_match(regime: Optional[str]):
    if regime is None:
        return ThresholdOverrideCrypto.regime.is_(None)
    return ThresholdOverrideCrypto.regime == regime


# ---------------------------------------------------------------------------
# Backfill what-if PnL — called by the analyzer post-trade
# ---------------------------------------------------------------------------


def backfill_shadow_pnl(
    engine: Any,
    *,
    override_id: int,
    what_if_pnl: float,
    now: Optional[dt.datetime] = None,
) -> bool:
    """Update an active shadow override with the what-if cumulative pnl."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        row = session.get(ThresholdOverrideCrypto, override_id)
        if row is None or not row.shadow:
            return False
        row.shadow_what_if_pnl = float(what_if_pnl)
        session.commit()
        return True
