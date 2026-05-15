"""Per-strategy regime protocols (v4 Phase A).

Maps (regime, strategy_id) → action dict using
``policy/regime_protocols_v1.json``. The risk precheck reads the
``size_multiplier`` and ``new_entries`` flags to throttle / halt
intents at decision time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.risk import DEFAULT_POLICY_DIR


@dataclass(frozen=True)
class RegimeProtocol:
    strategy_id: str
    regime: str
    size_multiplier: float
    new_entries: bool
    close_all: bool
    close_calls: bool
    close_equity_pct: float
    rotate_to_treasury: bool
    gated_by_claude: bool
    raw: Mapping[str, Any]


def _load_policy(policy_dir: Path = DEFAULT_POLICY_DIR) -> Mapping[str, Any]:
    return json.loads(
        (policy_dir / "regime_protocols_v1.json").read_text()
    )


def resolve(
    *,
    strategy_id: str,
    regime: str,
    policy_dir: Path = DEFAULT_POLICY_DIR,
) -> RegimeProtocol:
    """Return the regime protocol for ``strategy_id`` at ``regime``.

    Unknown strategies / regimes fall back to a permissive
    (``size_multiplier=1.0, new_entries=True``) protocol — the kernel
    is not expected to silently throttle strategies that haven't
    declared a regime entry. The operator should add one explicitly
    when registering a new family.
    """
    policy = _load_policy(policy_dir)
    family = (policy.get("per_strategy_protocol") or {}).get(strategy_id, {})
    bucket = family.get(regime) or {}
    return RegimeProtocol(
        strategy_id=strategy_id,
        regime=regime,
        size_multiplier=float(bucket.get("size_multiplier", 1.0)),
        new_entries=bool(bucket.get("new_entries", True)),
        close_all=bool(bucket.get("close_all", False)),
        close_calls=bool(bucket.get("close_calls", False)),
        close_equity_pct=float(bucket.get("close_equity_pct", 0.0)),
        rotate_to_treasury=bool(bucket.get("rotate_to_treasury", False)),
        gated_by_claude=bool(bucket.get("gated_by_claude", False)),
        raw=dict(bucket),
    )


__all__ = ["RegimeProtocol", "resolve"]
