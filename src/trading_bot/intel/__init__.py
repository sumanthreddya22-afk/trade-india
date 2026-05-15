"""Strategy-facing intel feature loader (Phase A+).

Strategies read this module via
``policy/strategy_signal_features_v1.json`` to query specific
feature_ids (e.g. ``finra_short_interest_pct``) without knowing which
intel feed owns them. The loader returns ``None`` for unimplemented
features so strategies degrade gracefully.

Plan v4 contract: adding a new intel_feature_id to a strategy = new
strategy_version + new policy version + 7-day cooldown.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)


def _load_signal_features_policy(
    policy_dir: Path = DEFAULT_POLICY_DIR,
) -> Mapping[str, Any]:
    p = policy_dir / "strategy_signal_features_v1.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def features_for_strategy(
    strategy_id: str, *, policy_dir: Path = DEFAULT_POLICY_DIR,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(price_features, intel_features)`` for ``strategy_id``."""
    cfg = _load_signal_features_policy(policy_dir)
    strat = (cfg.get("strategies") or {}).get(strategy_id, {})
    return (
        tuple(strat.get("price_features", ())),
        tuple(strat.get("intel_features", ())),
    )


def feed_for_feature(
    feature_id: str, *, policy_dir: Path = DEFAULT_POLICY_DIR,
) -> Optional[str]:
    """Return the feed_id owning ``feature_id``, or None if unknown."""
    cfg = _load_signal_features_policy(policy_dir)
    catalog = cfg.get("available_intel_features") or {}
    entry = catalog.get(feature_id)
    return entry.get("feed_id") if entry else None


# ---- Runtime materialisation ---------------------------------------------

# Registry of available feed instances. Daemon assembles + injects this;
# tests pass a custom registry.
_FEED_REGISTRY: dict[str, Any] = {}


def register_feed(feed_id: str, feed_instance: Any) -> None:
    _FEED_REGISTRY[feed_id] = feed_instance


def get_feed(feed_id: str) -> Any:
    return _FEED_REGISTRY.get(feed_id)


def materialize_features(
    *,
    strategy_id: str,
    symbol: str,
    asof: Optional[dt.datetime] = None,
    policy_dir: Path = DEFAULT_POLICY_DIR,
    feed_registry: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return ``{feature_id: value}`` for every intel_feature listed in
    the strategy's policy entry. Missing feed instances or query errors
    materialise as ``None`` rather than raising — strategies handle
    None gracefully."""
    asof = asof or dt.datetime.now(dt.timezone.utc)
    registry = feed_registry if feed_registry is not None else _FEED_REGISTRY
    _, intel_features = features_for_strategy(
        strategy_id, policy_dir=policy_dir,
    )
    out: dict[str, Any] = {}
    for fid in intel_features:
        feed_id = feed_for_feature(fid, policy_dir=policy_dir)
        if not feed_id:
            out[fid] = None
            continue
        feed = registry.get(feed_id)
        if feed is None or not hasattr(feed, "query_features"):
            out[fid] = None
            continue
        try:
            payload = feed.query_features(symbol, asof) or {}
            out[fid] = payload.get(fid)
        except Exception as e:  # noqa: BLE001
            log.warning("query_features %s/%s failed: %s", feed_id, fid, e)
            out[fid] = None
    return out


__all__ = [
    "feed_for_feature",
    "features_for_strategy",
    "get_feed",
    "materialize_features",
    "register_feed",
]
