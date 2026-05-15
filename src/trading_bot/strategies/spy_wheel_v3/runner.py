"""SPY Wheel v3 — multi-underlying daily-cadence runner.

Picks top-N optionable ETFs via ``policy/wheel_universe_v1.json``,
runs an independent wheel state machine per underlying, allocates
capital equal-weight, and emits up to N option intents per tick.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from trading_bot.ingest.universe import AssetFetcher, DiscoveryUnavailable
from trading_bot.research.universe_discovery import discover
from trading_bot.risk import DEFAULT_POLICY_DIR
from trading_bot.strategies.spy_wheel_v1.signal import (
    DEFAULT_PARAMS, occ_ticker, pick_expiry,
)
from trading_bot.strategies.spy_wheel_v3.state_machine import (
    WheelState, current_state, snapshot_underlying,
)

log = logging.getLogger(__name__)


STRATEGY_ID = "SPY_WHEEL_v3"
STRATEGY_VER = 3
POLICY_PATH = DEFAULT_POLICY_DIR / "wheel_universe_v1.json"
_FALLBACK_UNDERLYINGS = ("SPY", "QQQ", "IWM")


@dataclass(frozen=True)
class WheelV3Decision:
    decision_date: dt.date
    underlyings: tuple[str, ...]
    states: Mapping[str, str]
    intents: list[dict]
    equity: float
    capital_allocation_usd: Mapping[str, float]
    universe_payload: Optional[dict] = None
    # Dispatcher compatibility — see strategy_dispatch._dispatch_one
    # which references decision.target_weights on no-intents paths.
    target_weights: Mapping[str, float] = None
    universe: tuple[str, ...] = ()
    current_qty: Mapping[str, float] = None


def should_rebalance_today(today, last_date) -> bool:
    """Daily cadence — the per-underlying state machine handles
    cadence implicitly (no orders when state == SHORT_PUT_OPEN, etc.)."""
    return True


def _load_wheel_policy() -> dict:
    if not POLICY_PATH.exists():
        return {}
    try:
        return json.loads(POLICY_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def evaluate_strategy(
    *,
    decision_date: Optional[dt.date] = None,
    params: dict = DEFAULT_PARAMS,
    positions_fetcher: Optional[Callable[[], list[dict]]] = None,
    account_fetcher: Optional[Callable[[], dict]] = None,
    asset_fetcher: Optional[AssetFetcher] = None,
    volume_provider: Optional[Callable[[str], float | None]] = None,
    option_chain_fetcher: Optional[Callable] = None,
    option_expirations_fetcher: Optional[Callable] = None,
    contract_picker: Optional[Callable] = None,
) -> WheelV3Decision:
    """Generate intents per active underlying.

    The chain/expiry/contract callables are dependency-injected so tests
    can swap in deterministic fixtures. Production daemons pass the
    yfinance-backed fetchers from ``ingest.data_router``.
    """
    decision_date = decision_date or dt.date.today()
    policy = _load_wheel_policy()
    max_concurrent = int(policy.get("max_concurrent_wheels", 3))
    per_wheel_frac = float(
        policy.get("per_wheel_max_capital_fraction", 1.0 / max(1, max_concurrent))
    )

    try:
        ru = discover(
            strategy_id=STRATEGY_ID, policy_path=POLICY_PATH,
            asset_fetcher=asset_fetcher, volume_provider=volume_provider,
            decision_date=decision_date,
            fallback_symbols=_FALLBACK_UNDERLYINGS,
        )
        underlyings = ru.symbols[:max_concurrent]
        universe_payload = dict(ru.payload)
    except DiscoveryUnavailable as e:
        log.warning("spy_wheel_v3 discovery unavailable: %s", e)
        underlyings = _FALLBACK_UNDERLYINGS[:max_concurrent]
        universe_payload = {"_error": str(e), "symbols": list(underlyings)}

    positions = (positions_fetcher() or []) if positions_fetcher else []
    states: dict[str, str] = {}
    for u in underlyings:
        states[u] = current_state(positions, u).value

    equity = 0.0
    options_bp = 0.0
    if account_fetcher is not None:
        try:
            acct = account_fetcher() or {}
            equity = float(acct.get("equity", 0.0))
            options_bp = float(acct.get("options_buying_power", 0.0))
        except Exception:  # noqa: BLE001
            pass

    capital_alloc: dict[str, float] = {}
    intents: list[dict] = []
    if not underlyings:
        return WheelV3Decision(
            decision_date=decision_date,
            underlyings=tuple(underlyings),
            states=states,
            intents=intents, equity=equity,
            capital_allocation_usd=capital_alloc,
            universe_payload=universe_payload,
        )

    per_wheel_capital = (options_bp or equity) * per_wheel_frac

    # Lazy-import chain fetchers only when wired (yfinance is heavy +
    # network-bound). Tests pass the optional callables explicitly.
    if option_chain_fetcher is None or option_expirations_fetcher is None:
        try:
            from trading_bot.ingest.data_router import (
                fetch_option_chain, list_option_expirations,
            )
            option_chain_fetcher = option_chain_fetcher or fetch_option_chain
            option_expirations_fetcher = (
                option_expirations_fetcher or list_option_expirations
            )
        except Exception:  # noqa: BLE001
            # No chain fetcher → emit no intents; universe + state are still
            # captured in the decision for postmortem.
            option_chain_fetcher = None
            option_expirations_fetcher = None

    if contract_picker is None:
        try:
            from trading_bot.ingest.yfinance_adapter import (
                find_contract_by_delta,
            )
            contract_picker = find_contract_by_delta
        except Exception:  # noqa: BLE001
            contract_picker = None

    for u in underlyings:
        capital_alloc[u] = per_wheel_capital
        if option_chain_fetcher is None:
            continue
        state_val = states[u]
        if state_val not in (WheelState.FLAT.value, WheelState.LONG_STOCK.value):
            # SHORT_PUT_OPEN or SHORT_CALL_OPEN → wait for expiry.
            continue
        side = "put" if state_val == WheelState.FLAT.value else "call"

        try:
            expiries = option_expirations_fetcher(u)
        except Exception as e:  # noqa: BLE001
            log.info("wheel_v3: no expirations for %s: %s", u, e)
            continue
        expiry = pick_expiry(
            expiries, today=decision_date,
            target_days=int(params.get("dte_target_days", DEFAULT_PARAMS["dte_target_days"])),
            min_days=int(params.get("dte_min_days", DEFAULT_PARAMS["dte_min_days"])),
            max_days=int(params.get("dte_max_days", DEFAULT_PARAMS["dte_max_days"])),
        )
        if expiry is None:
            continue
        try:
            chain = option_chain_fetcher(u, expiry)
        except Exception as e:  # noqa: BLE001
            log.info("wheel_v3: chain fetch %s failed: %s", u, e)
            continue
        if chain is None or chain.underlying_price <= 0:
            continue

        target_delta = float(params.get("target_delta", DEFAULT_PARAMS["target_delta"]))
        try:
            contract = contract_picker(
                chain, side=side, target_delta=target_delta,
                risk_free_rate=float(params.get("risk_free_rate", DEFAULT_PARAMS["risk_free_rate"])),
            ) if contract_picker else None
        except Exception:  # noqa: BLE001
            contract = None
        if contract is None:
            continue

        snap = snapshot_underlying(positions, u)
        if side == "call":
            max_qty = int(snap.shares // 100)
        else:
            notional = contract.strike * 100.0
            max_qty = int(per_wheel_capital // notional) if notional > 0 else 0
        qty = max(0, min(
            max_qty,
            int(params.get("max_contracts_per_week",
                           DEFAULT_PARAMS["max_contracts_per_week"])),
        ))
        if qty <= 0:
            continue

        occ = occ_ticker(u, expiry, side, contract.strike)
        intents.append({
            "strategy_id": STRATEGY_ID,
            "strategy_ver": STRATEGY_VER,
            "symbol": occ,
            "side": "sell",
            "qty": float(qty),
            "intent_price": (
                contract.mid if contract.mid > 0 else contract.last_price
            ),
            "asset_class": "us_option",
            "lane": "options_income_wheel",
            "rationale": (
                f"wheel_v3 {u} state={state_val} → sell {qty} {side}@"
                f"{contract.strike} exp {expiry.isoformat()}"
            ),
            "_wheel_underlying": u,
            "_wheel_state": state_val,
            "_wheel_strike": contract.strike,
            "_wheel_expiry": expiry.isoformat(),
        })

    return WheelV3Decision(
        decision_date=decision_date,
        underlyings=tuple(underlyings),
        states=states, intents=intents,
        equity=equity,
        capital_allocation_usd=capital_alloc,
        universe_payload=universe_payload,
        target_weights={u: per_wheel_frac for u in underlyings},
        universe=tuple(underlyings),
        current_qty={},
    )


# StrategyDecision-compatible alias the dispatcher expects.
StrategyDecision = WheelV3Decision


__all__ = [
    "POLICY_PATH",
    "STRATEGY_ID",
    "STRATEGY_VER",
    "StrategyDecision",
    "WheelV3Decision",
    "evaluate_strategy",
    "should_rebalance_today",
]
