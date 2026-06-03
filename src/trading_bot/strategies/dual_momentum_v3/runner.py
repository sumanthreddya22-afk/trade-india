"""Dual Momentum v3 — daily cadence, sleeve-locked universe."""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from trading_bot.ingest.universe import AssetFetcher, DiscoveryUnavailable
from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, load_bars, open_store,
)
from trading_bot.research.universe_discovery import discover_sleeves
from trading_bot.risk import DEFAULT_POLICY_DIR
from trading_bot.strategies.dual_momentum_v1.signal import (
    DEFAULT_PARAMS, signal_fn,
)

log = logging.getLogger(__name__)

STRATEGY_ID = "DUAL_MOMENTUM_v3_INDIA"
STRATEGY_VER = 3
POLICY_PATH = DEFAULT_POLICY_DIR / "dual_momentum_sleeves_v1.json"

_FALLBACK_PER_SLEEVE: dict[str, tuple[str, ...]] = {
    "equity": ("NIFTYBEES",),
    "treasury": ("LIQUIDBEES",),
}


def _max_sleeve_pct() -> float:
    try:
        lock = json.loads((DEFAULT_POLICY_DIR / "risk_policy.lock").read_text())
    except Exception:
        return 0.04
    per_sym = float(lock.get("symbol", {}).get("per_symbol_gross_max_pct", 5.0))
    per_lane = float(lock.get("lane", {}).get("per_lane_allocation_max_pct", 40.0))
    asset = float(lock.get("asset_class", {}).get("equity_gross_max_pct", 80.0))
    return max(0.0, min(per_sym, per_lane, asset) * 0.90) / 100.0


@dataclass(frozen=True)
class StrategyDecision:
    decision_date: dt.date
    target_weights: dict[str, float]
    current_qty: dict[str, float]
    equity: float
    intents: list[dict]
    universe: tuple[str, ...] = ()
    universe_payload: Optional[dict] = None


def should_rebalance_today(today, last_date) -> bool:
    return True


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
    decision_date = decision_date or dt.date.today()
    if not historical_db.exists():
        return StrategyDecision(
            decision_date=decision_date, target_weights={},
            current_qty={}, equity=0.0, intents=[], universe=(),
            universe_payload={},
        )

    sleeves = discover_sleeves(
        strategy_id=STRATEGY_ID, policy_path=POLICY_PATH,
        asset_fetcher=asset_fetcher, volume_provider=volume_provider,
        decision_date=decision_date,
        fallback_per_sleeve=_FALLBACK_PER_SLEEVE,
    )

    # Stitch sleeves into a single universe in declared order.
    universe: list[str] = []
    sleeve_payloads: dict[str, dict] = {}
    for sname, ru in sleeves.items():
        for sym in ru.symbols:
            if sym not in universe:
                universe.append(sym)
        sleeve_payloads[sname] = dict(ru.payload)

    if not universe:
        return StrategyDecision(
            decision_date=decision_date, target_weights={},
            current_qty={}, equity=0.0, intents=[], universe=(),
            universe_payload={"sleeves": sleeve_payloads, "_error": "no_universe"},
        )

    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    min_hist = int(params.get("min_history_days", DEFAULT_PARAMS["min_history_days"]))
    start = decision_date - dt.timedelta(
        days=int(max(lookback, min_hist) * 1.5) + 30,
    )
    conn = open_store(historical_db)
    try:
        bars = load_bars(conn, symbols=tuple(universe), start=start, end=decision_date)
    finally:
        conn.close()

    target_weights = signal_fn(
        bars, decision_date, params=params, universe=tuple(universe),
    )

    current_qty: dict[str, float] = {}
    if positions_fetcher is not None:
        for p in positions_fetcher() or []:
            current_qty[p["symbol"]] = float(p.get("qty", 0))
    equity = 0.0
    if account_fetcher is not None:
        equity = float((account_fetcher() or {}).get("equity", 0.0))

    intents: list[dict] = []
    if target_weights and equity > 0:
        sleeve_cap = _max_sleeve_pct()
        close_by_sym: dict[str, float] = {}
        for sym, series in bars.items():
            relevant = [b for b in series if b.bar_date <= decision_date]
            if relevant:
                close_by_sym[sym] = relevant[-1].close

        for sym, w in target_weights.items():
            close = close_by_sym.get(sym)
            if not close or close <= 0:
                continue
            target_qty = (equity * sleeve_cap * w) / close
            diff = target_qty - current_qty.get(sym, 0.0)
            if abs(diff) < 1e-3:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": STRATEGY_VER,
                "symbol": sym,
                "side": "buy" if diff > 0 else "sell",
                "qty": abs(diff), "intent_price": close,
                "asset_class": "nse_equity", "lane": "dual_momentum",
                "rationale": f"dual_momentum_v3 winner: {sym} (weight={w:.3f})",
            })
        for sym, qty in current_qty.items():
            if sym in target_weights or qty <= 0 or sym not in universe:
                continue
            close = close_by_sym.get(sym, 0.0)
            if close <= 0:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": STRATEGY_VER,
                "symbol": sym, "side": "sell", "qty": qty,
                "intent_price": close, "asset_class": "nse_equity",
                "lane": "dual_momentum",
                "rationale": "dual_momentum_v3: rotate out",
            })

    return StrategyDecision(
        decision_date=decision_date,
        target_weights=dict(target_weights),
        current_qty=current_qty, equity=equity, intents=intents,
        universe=tuple(universe),
        universe_payload={"sleeves": sleeve_payloads, "symbols": universe},
    )


__all__ = [
    "POLICY_PATH", "STRATEGY_ID", "STRATEGY_VER", "StrategyDecision",
    "evaluate_strategy", "should_rebalance_today",
]
