"""ETF Momentum v3 — daily cadence, policy-locked universe."""
from __future__ import annotations

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
# Re-use v2 signal — same factor formula, daily-cadence universe change.
from trading_bot.strategies.etf_momentum_v1.signal import (
    DEFAULT_PARAMS, signal_fn,
)

log = logging.getLogger(__name__)

STRATEGY_ID = "ETF_MOMENTUM_v3"
STRATEGY_VER = 3
POLICY_PATH = DEFAULT_POLICY_DIR / "etf_universe_v1.json"

# Fallback universe used only when no asset_fetcher is wired (tests +
# backtest replay). Production daemons MUST inject the Alpaca fetcher.
_FALLBACK_UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "EFA", "EEM",
    "XLK", "XLF", "XLE", "XLV",
)


@dataclass(frozen=True)
class StrategyDecision:
    decision_date: dt.date
    target_weights: dict[str, float]
    current_qty: dict[str, float]
    equity: float
    intents: list[dict]
    universe: tuple[str, ...] = ()
    universe_payload: Optional[dict] = None


def should_rebalance_today(
    today: dt.date, last_decision_date: Optional[dt.date],
) -> bool:
    """Daily cadence — always rebalance."""
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
            decision_date=decision_date,
            target_weights={}, current_qty={}, equity=0.0, intents=[],
            universe=(), universe_payload={},
        )

    try:
        ru = discover(
            strategy_id=STRATEGY_ID,
            policy_path=POLICY_PATH,
            asset_fetcher=asset_fetcher,
            volume_provider=volume_provider,
            decision_date=decision_date,
            fallback_symbols=_FALLBACK_UNIVERSE,
        )
    except DiscoveryUnavailable as e:
        log.warning("etf_momentum_v3: discovery unavailable (%s)", e)
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
    skip = int(params.get("skip_recent_days", DEFAULT_PARAMS["skip_recent_days"]))
    start = decision_date - dt.timedelta(days=lookback + skip + 30)

    conn = open_store(historical_db)
    try:
        bars = load_bars(conn, symbols=ru.symbols, start=start, end=decision_date)
    finally:
        conn.close()

    target_weights = signal_fn(
        bars, decision_date, params=params, universe=ru.symbols,
    )

    current_qty: dict[str, float] = {}
    equity = 0.0
    if positions_fetcher is not None:
        for p in positions_fetcher() or []:
            sym = p.get("symbol", "")
            if sym:
                current_qty[sym] = float(p.get("qty", 0))
    if account_fetcher is not None:
        equity = float((account_fetcher() or {}).get("equity", 0.0))

    intents: list[dict] = []
    if target_weights and equity > 0:
        close_by_sym: dict[str, float] = {}
        for sym, series in bars.items():
            relevant = [b for b in series if b.bar_date <= decision_date]
            if relevant:
                close_by_sym[sym] = relevant[-1].close

        for sym, w in target_weights.items():
            close = close_by_sym.get(sym)
            if not close or close <= 0:
                continue
            target_qty = (equity * w) / close
            diff = target_qty - current_qty.get(sym, 0.0)
            if abs(diff) < 1e-3:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": STRATEGY_VER,
                "symbol": sym,
                "side": "buy" if diff > 0 else "sell",
                "qty": abs(diff), "intent_price": close,
                "asset_class": "us_equity", "lane": "etf_momentum",
                "rationale": f"etf_momentum_v3: rebalance to weight={w:.3f}",
            })
        for sym, qty in current_qty.items():
            if sym in target_weights or qty <= 0 or sym not in ru.symbols:
                continue
            close = close_by_sym.get(sym, 0.0) or 0.0
            if close <= 0:
                continue
            intents.append({
                "strategy_id": STRATEGY_ID,
                "strategy_ver": STRATEGY_VER,
                "symbol": sym, "side": "sell", "qty": qty,
                "intent_price": close, "asset_class": "us_equity",
                "lane": "etf_momentum",
                "rationale": "etf_momentum_v3: dropped from target",
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
]
