"""Phase A — data-driven universe discovery.

Replaces hardcoded symbol allowlists with policy-locked structured
filters. Each v3 strategy points at a policy JSON (e.g.
``policy/etf_universe_v1.json``) that defines the filter shape; this
module reads it, runs it against the daemon's asset_fetcher, ranks by
ADV, and returns a deterministic ``RankedUniverse``.

The discovery rule's full config is hashed into the
``universe_payload`` so the snapshot is reproducible and the boot
check can verify the strategy's universe came from a known filter.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from trading_bot.ingest.universe import (
    AssetFetcher, AssetRecord, DiscoveryUnavailable, enrich_with_volume,
)
from trading_bot.risk import DEFAULT_POLICY_DIR


REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class RankedUniverse:
    strategy_id: str
    rule_name: str
    rule_hash: str
    symbols: tuple[str, ...]
    payload: dict[str, Any]


def _load_policy(policy_path: Path) -> Mapping[str, Any]:
    if not policy_path.exists():
        raise DiscoveryUnavailable(f"policy missing: {policy_path}")
    try:
        return json.loads(policy_path.read_text())
    except json.JSONDecodeError as e:
        raise DiscoveryUnavailable(
            f"policy not valid JSON: {policy_path} ({e})"
        )


def _config_hash(cfg: Mapping[str, Any]) -> str:
    body = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _passes_filter(
    record: AssetRecord, policy: Mapping[str, Any],
) -> tuple[bool, str]:
    """Return (passed, reject_reason)."""
    if record.asset_class != policy.get("asset_class"):
        return False, "asset_class"
    if policy.get("tradable_required", True) and not record.tradable:
        return False, "not_tradable"
    must_have = tuple(policy.get("must_have_attributes", ()) or ())
    if must_have and not all(a in record.attributes for a in must_have):
        return False, f"missing_attr:{must_have}"
    excludes = tuple(policy.get("exclude_attributes", ()) or ())
    if any(a in record.attributes for a in excludes):
        return False, "excluded_attr"
    if record.avg_daily_volume_usd is None:
        return False, "no_volume_data"
    min_aum = float(policy.get("min_aum_usd", 0) or 0)
    if min_aum > 0:
        aum = getattr(record, "aum_usd", None)
        if aum is not None and aum < min_aum:
            return False, f"min_aum<{min_aum}"
    # Stablecoin filter (crypto)
    if policy.get("exclude_stablecoins"):
        prefixes = tuple(policy.get("stablecoin_symbol_prefixes", ()))
        for p in prefixes:
            if record.symbol.upper().startswith(p):
                return False, f"stablecoin:{p}"
    return True, ""


def discover(
    *,
    strategy_id: str,
    policy_path: Path,
    asset_fetcher: Optional[AssetFetcher],
    volume_provider: Optional[Callable[[str], float | None]] = None,
    decision_date: Optional[dt.date] = None,
    fallback_symbols: Sequence[str] = (),
) -> RankedUniverse:
    """Resolve a strategy's universe from a hash-locked policy file.

    Behaviour:
      * Loads the policy JSON.
      * Pulls assets via ``asset_fetcher`` for the policy's asset_class.
      * Enriches with volume if ``volume_provider`` is supplied.
      * Applies must-have / exclude attribute filters.
      * Drops anything below the AUM / volume thresholds.
      * Ranks by avg_daily_volume_usd descending.
      * Returns the top-N per the policy.
      * If asset_fetcher is None OR the filter zeros out, the discovery
        returns ``fallback_symbols`` and stamps the payload with a
        ``_fallback_reason`` breadcrumb. **Production daemons must
        supply asset_fetcher**; the fallback exists so tests + backtest
        replays still work.
    """
    decision_date = decision_date or dt.date.today()
    policy = _load_policy(policy_path)
    asset_class = policy["asset_class"]
    try:
        rel = str(policy_path.relative_to(REPO_ROOT))
    except ValueError:
        rel = str(policy_path)
    rule_name = f"discover:{strategy_id}:{policy_path.stem}"
    rule_hash = _config_hash(dict(policy))

    if asset_fetcher is None:
        return RankedUniverse(
            strategy_id=strategy_id,
            rule_name=rule_name,
            rule_hash=rule_hash,
            symbols=tuple(fallback_symbols),
            payload={
                "rule_name": rule_name,
                "rule_hash": rule_hash,
                "policy_path": rel,
                "decision_date": decision_date.isoformat(),
                "symbols": list(fallback_symbols),
                "_fallback_reason": "no asset_fetcher injected",
            },
        )

    try:
        records: Sequence[AssetRecord] = asset_fetcher(asset_class)
    except Exception as e:  # noqa: BLE001
        if not fallback_symbols:
            raise DiscoveryUnavailable(
                f"asset_fetcher failed for {asset_class}: {e}"
            )
        return RankedUniverse(
            strategy_id=strategy_id,
            rule_name=rule_name,
            rule_hash=rule_hash,
            symbols=tuple(fallback_symbols),
            payload={
                "rule_name": rule_name,
                "rule_hash": rule_hash,
                "policy_path": rel,
                "decision_date": decision_date.isoformat(),
                "symbols": list(fallback_symbols),
                "_fallback_reason": f"fetcher_error:{e}",
            },
        )

    if volume_provider is not None:
        records = enrich_with_volume(records, volume_provider)

    survivors: list[AssetRecord] = []
    reject_counts: dict[str, int] = {}
    for r in records:
        ok, reason = _passes_filter(r, policy)
        if ok:
            survivors.append(r)
        else:
            reject_counts[reason] = reject_counts.get(reason, 0) + 1

    if not survivors:
        min_size = int(policy.get("min_universe_size", 1) or 1)
        if not fallback_symbols:
            raise DiscoveryUnavailable(
                f"{rule_name}: zero survivors (rejects={reject_counts})"
            )
        return RankedUniverse(
            strategy_id=strategy_id,
            rule_name=rule_name,
            rule_hash=rule_hash,
            symbols=tuple(fallback_symbols),
            payload={
                "rule_name": rule_name,
                "rule_hash": rule_hash,
                "policy_path": rel,
                "decision_date": decision_date.isoformat(),
                "symbols": list(fallback_symbols),
                "_fallback_reason": f"zero_survivors:{reject_counts}",
            },
        )

    survivors.sort(key=lambda r: r.avg_daily_volume_usd or 0.0, reverse=True)
    top_n = int(
        policy.get("top_n_by_adv")
        or policy.get("top_n_by_dollar_volume")
        or policy.get("top_n_by_option_volume")
        or len(survivors)
    )
    chosen = tuple(r.symbol for r in survivors[:top_n])

    min_size = int(policy.get("min_universe_size", 1) or 1)
    if len(chosen) < min_size and not fallback_symbols:
        raise DiscoveryUnavailable(
            f"{rule_name}: chose {len(chosen)} symbols, "
            f"below min_universe_size={min_size}"
        )

    payload = {
        "rule_name": rule_name,
        "rule_hash": rule_hash,
        "policy_path": rel,
        "decision_date": decision_date.isoformat(),
        "symbols": list(chosen),
        "rejects": reject_counts,
        "n_candidates": len(records),
        "n_survivors": len(survivors),
    }
    return RankedUniverse(
        strategy_id=strategy_id,
        rule_name=rule_name,
        rule_hash=rule_hash,
        symbols=chosen,
        payload=payload,
    )


def discover_sleeves(
    *,
    strategy_id: str,
    policy_path: Path,
    asset_fetcher: Optional[AssetFetcher],
    volume_provider: Optional[Callable[[str], float | None]] = None,
    decision_date: Optional[dt.date] = None,
    fallback_per_sleeve: Mapping[str, Sequence[str]] = (),
) -> dict[str, RankedUniverse]:
    """Sleeve-aware discovery (Dual Momentum v3).

    Policy file has shape::

        {"asset_class": "us_equity",
         "sleeves": {"equity": {...filter...}, "treasury": {...filter...}}}

    Each sleeve gets its own ``RankedUniverse``; the caller stitches them.
    """
    decision_date = decision_date or dt.date.today()
    policy = _load_policy(policy_path)
    sleeves = policy.get("sleeves") or {}
    asset_class = policy.get("asset_class", "us_equity")
    fallback_per_sleeve = dict(fallback_per_sleeve)

    if asset_fetcher is None:
        return {
            name: RankedUniverse(
                strategy_id=strategy_id,
                rule_name=f"discover_sleeve:{strategy_id}:{name}",
                rule_hash=_config_hash(dict(filt) | {"asset_class": asset_class}),
                symbols=tuple(fallback_per_sleeve.get(name, ())),
                payload={
                    "sleeve": name,
                    "decision_date": decision_date.isoformat(),
                    "symbols": list(fallback_per_sleeve.get(name, ())),
                    "_fallback_reason": "no asset_fetcher injected",
                },
            )
            for name, filt in sleeves.items()
        }

    try:
        records: Sequence[AssetRecord] = asset_fetcher(asset_class)
    except Exception as e:  # noqa: BLE001
        records = ()
    if volume_provider is not None and records:
        records = enrich_with_volume(records, volume_provider)

    out: dict[str, RankedUniverse] = {}
    for sleeve_name, sleeve_filter in sleeves.items():
        merged = {**sleeve_filter, "asset_class": asset_class}
        sleeve_rule_hash = _config_hash(merged)
        rule_name = f"discover_sleeve:{strategy_id}:{sleeve_name}"
        fb = tuple(fallback_per_sleeve.get(sleeve_name, ()))
        survivors: list[AssetRecord] = []
        for r in records:
            ok, _ = _passes_filter(r, merged)
            if ok:
                # Apply fallback-classifier-allowlist if present (sleeve narrowing)
                allow = sleeve_filter.get("fallback_classifier_allowlist")
                if allow and r.symbol not in set(allow):
                    continue
                survivors.append(r)
        survivors.sort(
            key=lambda r: r.avg_daily_volume_usd or 0.0, reverse=True,
        )
        top_n = int(sleeve_filter.get("top_n_by_adv", 1))
        chosen = tuple(r.symbol for r in survivors[:top_n])
        if not chosen and fb:
            chosen = fb
            payload = {
                "sleeve": sleeve_name,
                "rule_hash": sleeve_rule_hash,
                "decision_date": decision_date.isoformat(),
                "symbols": list(chosen),
                "_fallback_reason": "zero_survivors_or_fetch_failed",
            }
        else:
            payload = {
                "sleeve": sleeve_name,
                "rule_hash": sleeve_rule_hash,
                "decision_date": decision_date.isoformat(),
                "symbols": list(chosen),
                "n_survivors": len(survivors),
            }
        out[sleeve_name] = RankedUniverse(
            strategy_id=strategy_id,
            rule_name=rule_name,
            rule_hash=sleeve_rule_hash,
            symbols=chosen,
            payload=payload,
        )
    return out


# ----- Universe audit helpers ----------------------------------------------

def compute_audit(
    *,
    strategy_id: str,
    current_members: Sequence[str],
    previous_members: Sequence[str] = (),
    breach_threshold_pct: float = 50.0,
) -> dict[str, Any]:
    """Diff current vs previous. Returns a dict suitable for the
    ``universe_audit_event`` writer.
    """
    cur = set(current_members)
    prev = set(previous_members)
    additions = sorted(cur - prev)
    removals = sorted(prev - cur)
    union = cur | prev
    if not union:
        turnover = 0.0
    else:
        turnover = 100.0 * (len(additions) + len(removals)) / len(union)
    breach = turnover >= breach_threshold_pct
    return {
        "strategy_id": strategy_id,
        "members": list(current_members),
        "additions": additions,
        "removals": removals,
        "turnover_pct": round(turnover, 2),
        "breach": breach,
    }


__all__ = [
    "RankedUniverse",
    "compute_audit",
    "discover",
    "discover_sleeves",
]
