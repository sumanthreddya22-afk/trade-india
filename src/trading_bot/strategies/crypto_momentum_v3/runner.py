"""Crypto Momentum v3 — daily cadence, policy-locked universe."""
from __future__ import annotations

RUNS_ON_NON_TRADING_DAYS = True

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from trading_bot.ingest.universe import AssetFetcher, DiscoveryUnavailable
from trading_bot.research.historical_bars import (
    DEFAULT_HISTORICAL_PATH, load_bars, open_store,
)
from trading_bot.research.universe_discovery import discover
from trading_bot.risk import DEFAULT_POLICY_DIR
from trading_bot.strategies.crypto_momentum_v1.signal import (
    CRYPTO_GROSS_MAX_PCT, DEFAULT_PARAMS, signal_fn,
)

log = logging.getLogger(__name__)

STRATEGY_ID = "CRYPTO_MOMENTUM_v3"
STRATEGY_VER = 3
POLICY_PATH = DEFAULT_POLICY_DIR / "crypto_universe_v1.json"

_FALLBACK_UNIVERSE: tuple[str, ...] = ("BTC/INR", "ETH/INR")


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


def _crypto_cap_pct() -> float:
    import json
    try:
        lock = json.loads(
            (DEFAULT_POLICY_DIR / "risk_policy.lock").read_text()
        )
        crypto = float(lock.get("asset_class", {})
                            .get("crypto_gross_max_pct", CRYPTO_GROSS_MAX_PCT))
        per_lane = float(lock.get("lane", {})
                              .get("per_lane_allocation_max_pct", 40.0))
        per_sym = float(lock.get("symbol", {})
                             .get("per_symbol_gross_max_pct", 5.0))
        return max(0.0, min(crypto, per_lane, per_sym) * 0.90)
    except Exception:
        return CRYPTO_GROSS_MAX_PCT * 0.90


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
            current_qty={}, equity=0.0, intents=[],
            universe=(), universe_payload={},
        )

    try:
        ru = discover(
            strategy_id=STRATEGY_ID, policy_path=POLICY_PATH,
            asset_fetcher=asset_fetcher, volume_provider=volume_provider,
            decision_date=decision_date,
            fallback_symbols=_FALLBACK_UNIVERSE,
        )
    except DiscoveryUnavailable as e:
        log.warning("crypto_momentum_v3: discovery unavailable (%s)", e)
        return StrategyDecision(
            decision_date=decision_date, target_weights={},
            current_qty={}, equity=0.0, intents=[],
            universe=(), universe_payload={"_error": str(e)},
        )

    if not ru.symbols:
        return StrategyDecision(
            decision_date=decision_date, target_weights={},
            current_qty={}, equity=0.0, intents=[],
            universe=(), universe_payload=ru.payload,
        )

    lookback = int(params.get("lookback_days", DEFAULT_PARAMS["lookback_days"]))
    start = decision_date - dt.timedelta(days=lookback + 30)
    conn = open_store(historical_db)
    try:
        bars = load_bars(conn, symbols=ru.symbols, start=start, end=decision_date)
    finally:
        conn.close()

    target_weights = signal_fn(bars, decision_date, params=params)

    current_qty: dict[str, float] = {}
    equity = 0.0
    if positions_fetcher is not None:
        for p in positions_fetcher() or []:
            sym = p["symbol"]
            if "/" not in sym and sym.endswith("USD") and len(sym) >= 6:
                sym = sym[:-3] + "/USD"
            current_qty[sym] = float(p.get("qty", 0))
    if account_fetcher is not None:
        equity = float((account_fetcher() or {}).get("equity", 0.0))

    intents: list[dict] = []
    if target_weights and equity > 0:
        crypto_cap_pct = _crypto_cap_pct()
        crypto_sleeve_value = equity * crypto_cap_pct / 100.0

        close_by_sym: dict[str, float] = {}
        for sym, series in bars.items():
            relevant = [b for b in series if b.bar_date <= decision_date]
            if relevant:
                close_by_sym[sym] = relevant[-1].close

        for sym, w_sleeve in target_weights.items():
            close = close_by_sym.get(sym)
            if not close or close <= 0:
                continue
            target_qty = round(
                (crypto_sleeve_value * w_sleeve) / close, 6,
            )
            diff = target_qty - current_qty.get(sym, 0.0)
            if abs(diff) < 1e-5:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": STRATEGY_VER,
                "symbol": sym,
                "side": "buy" if diff > 0 else "sell",
                "qty": abs(diff), "intent_price": close,
                "asset_class": "crypto", "lane": "crypto_trend",
                "rationale": f"crypto_momentum_v3: {sym} weight={w_sleeve:.3f}",
            })
        for sym, qty in current_qty.items():
            if sym in target_weights or qty <= 0 or sym not in ru.symbols:
                continue
            close = close_by_sym.get(sym, 0.0)
            if close <= 0:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": STRATEGY_VER,
                "symbol": sym, "side": "sell", "qty": qty,
                "intent_price": close, "asset_class": "crypto",
                "lane": "crypto_trend",
                "rationale": "crypto_momentum_v3: rotate out",
            })

    return StrategyDecision(
        decision_date=decision_date,
        target_weights=dict(target_weights),
        current_qty=current_qty, equity=equity, intents=intents,
        universe=ru.symbols, universe_payload=dict(ru.payload),
    )


__all__ = [
    "POLICY_PATH", "STRATEGY_ID", "STRATEGY_VER", "StrategyDecision",
    "evaluate_strategy", "should_rebalance_today",
    "RUNS_ON_NON_TRADING_DAYS",
]
