"""Manual regime-override loader (v4 Phase A).

Operator-signed override file at ``policy/manual_regime_lock``. When
present + not expired, the classifier's verdict is ignored and the
``forced_regime`` is honoured for every asset class in
``asset_class_scope``. **Manual override always wins.**
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManualOverride:
    forced_regime: Optional[str]
    asset_class_scope: tuple[str, ...]
    reason_md: str
    expiry: Optional[dt.datetime]
    raw: dict


def load(
    policy_dir: Path = DEFAULT_POLICY_DIR,
    *,
    now: Optional[dt.datetime] = None,
) -> Optional[ManualOverride]:
    p = policy_dir / "manual_regime_lock"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        log.warning("manual_regime_lock not valid JSON: %s", e)
        return None
    forced = raw.get("forced_regime")
    if forced is None:
        # Explicit null release → no override.
        return None
    expiry_iso = raw.get("expiry_iso")
    expiry: Optional[dt.datetime] = None
    if expiry_iso:
        try:
            expiry = dt.datetime.fromisoformat(expiry_iso.replace("Z", "+00:00"))
        except ValueError:
            log.warning("manual_regime_lock expiry not parseable: %s", expiry_iso)
            return None
    now = now or dt.datetime.now(dt.timezone.utc)
    if expiry is not None and now >= expiry:
        return None
    scope = tuple(raw.get("asset_class_scope") or ("stocks", "crypto", "options"))
    return ManualOverride(
        forced_regime=forced,
        asset_class_scope=scope,
        reason_md=raw.get("reason_md", ""),
        expiry=expiry,
        raw=raw,
    )


def applies_to(
    override: Optional[ManualOverride], asset_class: str,
) -> bool:
    if override is None:
        return False
    return asset_class in override.asset_class_scope


__all__ = ["ManualOverride", "applies_to", "load"]
