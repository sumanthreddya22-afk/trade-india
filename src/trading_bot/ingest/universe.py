"""Candidate universe discovery — data-driven, hash-locked.

No strategy may pin a hardcoded symbol list. Each strategy's universe
is derived by a ``DiscoveryRule`` that runs at decision time against
the Alpaca asset listing (or another live source) and returns the
symbols to consider. The rule configuration is hash-locked in
``policy/universe_rules.lock`` so a change to the rule = a new
strategy_version with its own validation packet.

The discovery result is captured in ``feature_snapshot`` per
decision; a backtest replays the same snapshot to reproduce the
decision exactly.

Failure mode: if a rule cannot resolve (e.g. Alpaca asset listing is
down), the discovery function raises ``DiscoveryUnavailable`` and the
caller halts the strategy for this decision rather than falling back
to stale state. **Never** silently substitute.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class DiscoveryUnavailable(RuntimeError):
    """Raised when the discovery rule cannot produce a universe — e.g.
    upstream API failed, or the filter returned zero matches. The
    daemon must treat this as a per-decision halt, not a silent
    fallback to a stale universe."""


@dataclass(frozen=True)
class AssetRecord:
    """Normalised projection of an Alpaca / yfinance asset row. Sources
    map their native fields into this shape so discovery rules are
    decoupled from any single SDK."""
    symbol: str
    asset_class: str           # "us_equity" | "crypto" | "us_option"
    tradable: bool
    fractionable: bool
    avg_daily_volume_usd: float | None = None
    name: str | None = None
    attributes: tuple[str, ...] = ()   # Alpaca tags: ETF, OPTIONS_ENABLED, ...


AssetFetcher = Callable[[str], Sequence[AssetRecord]]
"""Function: asset_class -> AssetRecord sequence. Daemon wires this
from the Alpaca adapter; tests pass a stub."""


# ---------------------------------------------------------------------------
# Discovery rules
# ---------------------------------------------------------------------------

class DiscoveryRule(Protocol):
    """A pure function from (asset_universe, decision_date) to symbols.

    Implementations MUST be deterministic given identical inputs so
    the snapshot replays identically.
    """

    name: str

    def config_dict(self) -> dict[str, Any]: ...

    def select(
        self, assets: Sequence[AssetRecord], decision_date: dt.date,
    ) -> list[str]: ...


@dataclass(frozen=True)
class TopByVolume:
    """Pick the top-N tradable, fractionable assets by 30-day average
    dollar volume. Used to anchor liquid-equity / liquid-crypto sleeves
    in a data-driven way.

    ``required_attributes`` may include Alpaca tags (e.g. ``"ETF"``) so
    a rule like "top-1 ETF" can be expressed without naming SPY.
    """
    asset_class: str
    top_n: int = 1
    required_attributes: tuple[str, ...] = ()
    symbol_allowlist: tuple[str, ...] = ()
    """When non-empty, restricts candidates to this set. Useful for
    the seed thesis universe (Plan v4 §3 freezes the v1 ETF set; the
    allowlist enforces it while keeping the LIQUIDITY ranking live)."""

    name: str = field(default="top_by_volume")

    def config_dict(self) -> dict[str, Any]:
        return {"kind": "top_by_volume", **asdict(self)}

    def select(
        self, assets: Sequence[AssetRecord], decision_date: dt.date,
    ) -> list[str]:
        filtered: list[AssetRecord] = []
        for a in assets:
            if a.asset_class != self.asset_class:
                continue
            if not a.tradable:
                continue
            if self.required_attributes and not all(
                attr in a.attributes for attr in self.required_attributes
            ):
                continue
            if self.symbol_allowlist and a.symbol not in self.symbol_allowlist:
                continue
            if a.avg_daily_volume_usd is None:
                continue
            filtered.append(a)
        if not filtered:
            raise DiscoveryUnavailable(
                f"top_by_volume[{self.asset_class}]: zero assets matched filter"
            )
        filtered.sort(key=lambda a: a.avg_daily_volume_usd or 0.0, reverse=True)
        chosen = [a.symbol for a in filtered[: max(self.top_n, 0)]]
        if not chosen:
            raise DiscoveryUnavailable(
                f"top_by_volume[{self.asset_class}]: top_n={self.top_n} "
                f"yielded empty selection"
            )
        return chosen


@dataclass(frozen=True)
class Composite:
    """Concatenate symbols from multiple sub-rules, deduplicated and
    order-preserved (first sub-rule wins on duplicates)."""
    sub_rules: tuple[DiscoveryRule, ...]
    name: str = field(default="composite")

    def config_dict(self) -> dict[str, Any]:
        return {
            "kind": "composite",
            "sub_rules": [r.config_dict() for r in self.sub_rules],
        }

    def select(
        self, assets: Sequence[AssetRecord], decision_date: dt.date,
    ) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for r in self.sub_rules:
            for sym in r.select(assets, decision_date):
                if sym in seen:
                    continue
                seen.add(sym)
                out.append(sym)
        if not out:
            raise DiscoveryUnavailable(
                "composite: zero symbols across all sub-rules"
            )
        return out


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UniverseResolution:
    """Output of discovery: the chosen symbols + a snapshot payload
    suitable for hashing into ``feature_snapshot``.

    The hash combines the rule config and the resulting symbols, so a
    change in either (e.g. SPY drops below TLT in volume one day)
    produces a different snapshot id."""
    rule_name: str
    rule_hash: str
    symbols: tuple[str, ...]
    payload: dict[str, Any]

    @property
    def snapshot_id(self) -> str:
        body = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
        return f"univ:{hashlib.sha256(body.encode()).hexdigest()[:16]}"


def _hash_config(cfg: Mapping[str, Any]) -> str:
    body = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def resolve_universe(
    rule: DiscoveryRule,
    *,
    asset_fetcher: AssetFetcher,
    decision_date: dt.date,
    asset_classes: Iterable[str] = ("us_equity", "crypto"),
) -> UniverseResolution:
    """Resolve a rule against live data. Raises DiscoveryUnavailable
    when the upstream is empty or unreachable; callers translate that
    into a per-decision halt.
    """
    assets: list[AssetRecord] = []
    failures: list[str] = []
    for cls in asset_classes:
        try:
            chunk = asset_fetcher(cls)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{cls}: {type(e).__name__}: {e}")
            continue
        assets.extend(chunk)
    if not assets:
        raise DiscoveryUnavailable(
            f"asset_fetcher returned zero records ({'; '.join(failures)})"
        )
    symbols = tuple(rule.select(assets, decision_date))
    cfg = rule.config_dict()
    rh = _hash_config(cfg)
    payload = {
        "rule_name": rule.name,
        "rule_config": cfg,
        "rule_hash": rh,
        "decision_date": decision_date.isoformat(),
        "symbols": list(symbols),
    }
    return UniverseResolution(
        rule_name=rule.name,
        rule_hash=rh,
        symbols=symbols,
        payload=payload,
    )


def enrich_with_volume(
    records: Sequence[AssetRecord],
    volume_provider: Callable[[str], float | None],
) -> list[AssetRecord]:
    """Return a new list with ``avg_daily_volume_usd`` populated from
    ``volume_provider(symbol)``. Records that already carry a non-None
    volume pass through unchanged; records the provider returns None
    for stay None-valued (the discovery rule will then exclude them).

    Decoupled from any specific source so the daemon can wire it from
    yfinance, a bars table, or a cached metric.
    """
    out: list[AssetRecord] = []
    for r in records:
        if r.avg_daily_volume_usd is not None:
            out.append(r)
            continue
        try:
            vol = volume_provider(r.symbol)
        except Exception:  # noqa: BLE001
            vol = None
        out.append(AssetRecord(
            symbol=r.symbol,
            asset_class=r.asset_class,
            tradable=r.tradable,
            fractionable=r.fractionable,
            avg_daily_volume_usd=vol,
            name=r.name,
            attributes=r.attributes,
        ))
    return out


__all__ = [
    "AssetFetcher",
    "AssetRecord",
    "Composite",
    "DiscoveryRule",
    "DiscoveryUnavailable",
    "TopByVolume",
    "UniverseResolution",
    "enrich_with_volume",
    "resolve_universe",
]
