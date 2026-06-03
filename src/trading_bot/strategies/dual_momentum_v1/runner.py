"""Dual Momentum live runner.

The universe is **not** hardcoded — it is resolved at decision time
through ``trading_bot.ingest.universe``. The default discovery rule
returns today's most-liquid US equity ETF + most-liquid long
Treasury ETF restricted to the seed thesis allowlist. Today that
returns ("NIFTYBEES", "LIQUIDBEES"); if Vanguard or BlackRock launch a more liquid
broad-market ETF that's already in the allowlist, the rule picks it
without a code change.

The universe is captured per decision in ``feature_snapshot`` so a
backtest replays the same symbols. The discovery rule itself is
hash-locked: changing the rule = new strategy_version + new
validation packet (Plan v4 §13).
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from trading_bot.ingest.universe import (
    AssetFetcher, AssetRecord, Composite, DiscoveryUnavailable,
    TopByVolume, UniverseResolution, resolve_universe,
)
from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, load_bars, open_store,
)
from trading_bot.strategies.dual_momentum_v1.signal import (
    DEFAULT_PARAMS, STRATEGY_ID, signal_fn,
)
from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Universe discovery rule
# ---------------------------------------------------------------------------

# The seed-thesis universe lives in docs/edge_thesis_v1.md. We pin the
# allowlist here so the discovery rule cannot drift outside the
# validated set without an explicit code + validation-packet change.
_THESIS_EQUITY_ALLOWLIST = (
    "NIFTYBEES", "JUNIORBEES", "SETFNIF50", "HDFCNIFETF", "UTINIFTETF",
)
_THESIS_TREASURY_ALLOWLIST = ("LIQUIDBEES", "GILT5YBEES")

DISCOVERY_RULE = Composite(
    sub_rules=(
        TopByVolume(
            asset_class="nse_equity", top_n=1,
            required_attributes=("ETF",),
            symbol_allowlist=_THESIS_EQUITY_ALLOWLIST,
            name="dual_momentum_v1.equity_top1",
        ),
        TopByVolume(
            asset_class="nse_equity", top_n=1,
            required_attributes=("ETF",),
            symbol_allowlist=_THESIS_TREASURY_ALLOWLIST,
            name="dual_momentum_v1.treasury_top1",
        ),
    ),
    name="dual_momentum_v1.default",
)


def _max_sleeve_pct() -> float:
    """The most-binding cap among:
      * per_symbol_gross_max_pct  (most-binding for single-symbol strategies)
      * per_lane_allocation_max_pct  (40% default)
      * equity_gross_max_pct  (80% default)
    Apply a small safety buffer so we don't trip the kernel.
    """
    import json
    try:
        lock = json.loads((DEFAULT_POLICY_DIR / "risk_policy.lock").read_text())
    except Exception:
        return 0.04   # 4% safe default
    per_sym = float(lock.get("symbol", {}).get("per_symbol_gross_max_pct", 5.0))
    per_lane = float(lock.get("lane", {}).get("per_lane_allocation_max_pct", 40.0))
    asset = float(lock.get("asset_class", {}).get("equity_gross_max_pct", 80.0))
    most_binding = min(per_sym, per_lane, asset)
    # 10% buffer below the binding cap.
    return max(0.0, most_binding * 0.90) / 100.0


@dataclass(frozen=True)
class StrategyDecision:
    decision_date: dt.date
    target_weights: dict[str, float]
    current_qty: dict[str, float]
    equity: float
    intents: list[dict]
    universe: tuple[str, ...] = ()
    """Symbols actually evaluated this decision — captured for the
    feature_snapshot so a backtest can replay the exact universe."""
    universe_payload: dict = None
    """Discovery rule payload (rule_name, rule_hash, decision_date,
    symbols). ``None`` when no universe was resolved."""


def should_rebalance_today(today: dt.date, last_date: dt.date | None) -> bool:
    """Monthly cadence: first trading day we see this calendar month."""
    if last_date is None:
        return True
    return (today.year, today.month) != (last_date.year, last_date.month)


# Static fallback universe — used ONLY when no asset_fetcher is wired
# (unit tests + backtest replay). Production daemons must inject the
# Alpaca fetcher so the universe is data-driven.
_STATIC_FALLBACK_UNIVERSE: tuple[str, ...] = ("NIFTYBEES", "LIQUIDBEES")


def _resolve_universe_with_fallback(
    *,
    asset_fetcher: Optional[AssetFetcher],
    decision_date: dt.date,
    volume_provider: Optional[Callable[[str], float | None]] = None,
) -> tuple[tuple[str, ...], dict]:
    """Resolve the discovery rule.

    Alpaca's asset listing does not include average dollar volume.
    When ``volume_provider`` is supplied (the daemon wires this from
    historical bars) we enrich each record before passing to the
    discovery rule so the liquidity ranking is data-driven. Without
    it, every record's ADV stays None and the discovery rule excludes
    them — at which point we fall back to the static thesis universe
    rather than halting decisions during the rollout.
    """
    if asset_fetcher is None:
        return _STATIC_FALLBACK_UNIVERSE, {
            "rule_name": DISCOVERY_RULE.name,
            "rule_hash": "fallback:static",
            "decision_date": decision_date.isoformat(),
            "symbols": list(_STATIC_FALLBACK_UNIVERSE),
            "_fallback_reason": "no asset_fetcher injected",
        }
    fetcher: AssetFetcher = asset_fetcher
    if volume_provider is not None:
        from trading_bot.ingest.universe import enrich_with_volume
        base_fetcher = asset_fetcher

        def _enriched(asset_class: str):
            return enrich_with_volume(base_fetcher(asset_class), volume_provider)

        fetcher = _enriched
    try:
        res: UniverseResolution = resolve_universe(
            DISCOVERY_RULE,
            asset_fetcher=fetcher,
            decision_date=decision_date,
            asset_classes=("nse_equity",),
        )
    except DiscoveryUnavailable as e:
        log.warning(
            "dual_momentum_v1: discovery unavailable (%s); falling back "
            "to static thesis universe",
            e,
        )
        return _STATIC_FALLBACK_UNIVERSE, {
            "rule_name": DISCOVERY_RULE.name,
            "rule_hash": "fallback:discovery_unavailable",
            "decision_date": decision_date.isoformat(),
            "symbols": list(_STATIC_FALLBACK_UNIVERSE),
            "_fallback_reason": str(e),
        }
    return res.symbols, res.payload


def evaluate_strategy(
    *,
    historical_db: Path = DEFAULT_HISTORICAL_PATH,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
    asset_fetcher: Optional[AssetFetcher] = None,
    volume_provider: Optional[Callable[[str], float | None]] = None,
) -> StrategyDecision:
    """Evaluate Dual Momentum for ``decision_date``.

    ``asset_fetcher`` is injected by the daemon (wired from the
    Alpaca adapter's asset listing). When None, the runner falls
    back to a static thesis universe ONLY so unit tests + backtest
    replays work — a production daemon MUST supply one, so a missing
    fetcher in live mode raises ``DiscoveryUnavailable`` upstream
    rather than silently using stale symbols.

    ``volume_provider`` (symbol → avg-dollar-volume) is wired from
    the historical-bars store so the discovery rule can rank Alpaca
    asset records, which don't carry ADV themselves.
    """
    decision_date = decision_date or dt.date.today()
    if not historical_db.exists():
        return StrategyDecision(
            decision_date=decision_date,
            target_weights={}, current_qty={}, equity=0.0, intents=[],
            universe=(), universe_payload={},
        )

    # 1. Discover today's universe.
    universe, universe_payload = _resolve_universe_with_fallback(
        asset_fetcher=asset_fetcher, decision_date=decision_date,
        volume_provider=volume_provider,
    )
    if not universe:
        return StrategyDecision(
            decision_date=decision_date,
            target_weights={}, current_qty={}, equity=0.0, intents=[],
            universe=(), universe_payload=universe_payload or {},
        )

    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    min_hist = int(params.get("min_history_days", DEFAULT_PARAMS["min_history_days"]))
    # Need at least min_hist TRADING days. Calendar buffer = ×1.5 + 30
    # to cover weekends + market holidays.
    start = decision_date - dt.timedelta(days=int(max(lookback, min_hist) * 1.5) + 30)

    conn = open_store(historical_db)
    try:
        bars = load_bars(conn, symbols=universe, start=start, end=decision_date)
    finally:
        conn.close()

    target_weights = signal_fn(
        bars, decision_date, params=params, universe=universe,
    )

    current_qty: dict[str, float] = {}
    if positions_fetcher is not None:
        for p in positions_fetcher() or []:
            current_qty[p["symbol"]] = float(p.get("qty", 0))
    equity = 0.0
    if account_fetcher is not None:
        equity = float((account_fetcher() or {}).get("equity", 0.0))

    intents: list[dict] = []
    # Sleeve cap: most-binding cap from risk_policy.lock with a 10%
    # buffer. For single-symbol strategies on the v4 default lock this
    # is 4.5% (= 5% per_symbol × 0.9). Operator-tunable via the lock.
    sleeve_cap = _max_sleeve_pct()
    if target_weights and equity > 0:
        close_by_sym: dict[str, float] = {}
        for sym, series in bars.items():
            relevant = [b for b in series if b.bar_date <= decision_date]
            if relevant:
                close_by_sym[sym] = relevant[-1].close

        # Convert weights to qty diffs.
        for sym, w in target_weights.items():
            close = close_by_sym.get(sym)
            if not close or close <= 0:
                continue
            target_qty = (equity * sleeve_cap * w) / close
            diff = target_qty - current_qty.get(sym, 0.0)
            if abs(diff) < 1e-3:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID, "strategy_ver": 1,
                "symbol": sym, "side": "buy" if diff > 0 else "sell",
                "qty": abs(diff), "intent_price": close,
                "asset_class": "nse_equity", "lane": "dual_momentum",
                "rationale": f"dual-momentum winner: {sym} (weight={w:.3f})",
            })
        # Sell any held symbol not in target.
        for sym, qty in current_qty.items():
            if sym in target_weights or qty <= 0 or sym not in universe:
                continue
            close = close_by_sym.get(sym, 0.0)
            if close <= 0:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID, "strategy_ver": 1,
                "symbol": sym, "side": "sell", "qty": qty,
                "intent_price": close, "asset_class": "nse_equity",
                "lane": "dual_momentum",
                "rationale": "dual-momentum: rotate out",
            })

    return StrategyDecision(
        decision_date=decision_date,
        target_weights=dict(target_weights),
        current_qty=current_qty, equity=equity, intents=intents,
        universe=tuple(universe), universe_payload=universe_payload or {},
    )


__all__ = ["StrategyDecision", "evaluate_strategy", "should_rebalance_today"]
