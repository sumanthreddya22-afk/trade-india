"""Strategy dispatch — bridge from the daemon's strategy_runner job to
the individual strategy modules.

The dispatch loop:

  1. Read enabled strategies from ``strategy_version`` where status is
     in {tiny_paper, scaled_paper, live}.
  2. For each, look up the strategy module by ``strategy_id``.
  3. Decide whether today is a rebalance day (signal_runner.should_rebalance_today).
  4. Evaluate the signal — gets a ``StrategyDecision`` with intents.
  5. Submit each intent through ``execution.order_router.submit_order``.

The strategy_runner job stays small; this module owns the
"glue between registry and strategies" concern so it's testable on its
own and can grow without bloating the job function.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.daemon.jobs import DaemonContext

log = logging.getLogger(__name__)

# Mapping of registered strategy_id → importable module path.
# Kept explicit (not via setuptools entry points) so a strategy can't
# accidentally activate by being installed; the operator must add it
# here AND register the version row.
STRATEGY_MODULE = {
    "ETF_MOMENTUM_v1": "trading_bot.strategies.etf_momentum_v1",
}

# Statuses where the daemon will tick the strategy. Plan v4 §7.
LIVE_STATUSES = frozenset({"tiny_paper", "scaled_paper", "live"})


def _enabled_strategies(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Return ``[(strategy_id, strategy_ver), …]`` of strategies the
    daemon should tick. Reads the latest version per strategy_id."""
    try:
        cur = conn.execute(
            "SELECT strategy_id, MAX(strategy_ver) AS v, status "
            "FROM strategy_version GROUP BY strategy_id"
        )
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    out = []
    for sid, v, status in rows:
        if status in LIVE_STATUSES:
            out.append((sid, int(v)))
    return out


def dispatch_all_strategies(ctx: "DaemonContext") -> dict:
    """Tick every enabled strategy. Returns a summary dict for the
    heartbeat detail string."""
    from trading_bot.ledger import connect_writer
    conn = connect_writer(ctx.ledger_db)
    try:
        enabled = _enabled_strategies(conn)
    finally:
        conn.close()

    summary: dict = {"n_enabled": len(enabled), "details": []}
    for sid, ver in enabled:
        mod_path = STRATEGY_MODULE.get(sid)
        if mod_path is None:
            log.warning("dispatch: strategy %s registered but no module mapped", sid)
            summary["details"].append({"sid": sid, "status": "no_module"})
            continue
        try:
            result = _dispatch_one(ctx, sid, ver, mod_path)
            summary["details"].append({"sid": sid, **result})
        except Exception as e:  # noqa: BLE001
            log.exception("dispatch %s failed", sid)
            summary["details"].append({"sid": sid, "error": f"{type(e).__name__}: {e}"})
    return summary


def _dispatch_one(
    ctx: "DaemonContext", strategy_id: str, strategy_ver: int, module_path: str,
) -> dict:
    import importlib
    mod = importlib.import_module(module_path)
    # Read last decision date for cadence gating.
    from trading_bot.ledger import connect_writer
    conn = connect_writer(ctx.ledger_db)
    try:
        cur = conn.execute(
            "SELECT MAX(decision_ts) FROM strategy_decision WHERE strategy_id=?",
            (strategy_id,),
        )
        row = cur.fetchone()
        last_date = (
            dt.datetime.fromisoformat(row[0]).date()
            if row and row[0] else None
        )
    finally:
        conn.close()

    today = dt.date.today()
    # Strategy may export `should_rebalance_today`; else default monthly.
    runner = importlib.import_module(f"{module_path}.runner")
    if not runner.should_rebalance_today(today, last_date):
        return {"action": "skipped_cadence", "last_date": str(last_date)}

    decision = runner.evaluate_strategy(
        decision_date=today,
        positions_fetcher=ctx.positions_fetcher,
        account_fetcher=ctx.account_fetcher,
    )
    if not decision.intents:
        return {"action": "no_intents",
                "target_count": len(decision.target_weights)}

    submitted = 0
    rejected = 0
    for intent_dict in decision.intents:
        ok = _submit_one(ctx, intent_dict)
        if ok:
            submitted += 1
        else:
            rejected += 1
    return {"action": "submitted",
            "n_intents": len(decision.intents),
            "submitted": submitted, "rejected": rejected,
            "target_weights": decision.target_weights}


def _submit_one(ctx: "DaemonContext", intent_dict: dict) -> bool:
    """Translate the dict-shape intent → OrderIntent → order_router.

    Returns True iff the router accepted (verdict != halt). False
    indicates the risk kernel blocked; the rejection is already logged
    to strategy_decision by the router.
    """
    from trading_bot.execution.order_router import submit_order
    from trading_bot.ledger import OrderIntent, connect_writer
    from trading_bot.risk import load_policy
    from trading_bot.risk.types import AccountState, Position

    # Build OrderIntent
    client_order_id = f"{intent_dict['strategy_id']}-{uuid.uuid4().hex[:8]}"
    intent = OrderIntent(
        client_order_id=client_order_id,
        strategy_id=intent_dict["strategy_id"],
        strategy_ver=intent_dict.get("strategy_ver", 1),
        symbol=intent_dict["symbol"],
        asset_class=intent_dict.get("asset_class", "us_equity"),
        side=intent_dict["side"],
        qty=float(intent_dict["qty"]),
        limit_price=None,
        tif="day",
        origin=f"daemon:strategy_runner",
    )

    policy = load_policy()
    account = AccountState(
        equity=float((ctx.account_fetcher() or {}).get("equity", 0.0)),
        cash=float((ctx.account_fetcher() or {}).get("cash", 0.0)),
        buying_power=float((ctx.account_fetcher() or {}).get("buying_power", 0.0)),
        daily_session_pnl_pct=0.0,
        rolling_60d_drawdown_pct=0.0,
    )
    positions = [
        Position(
            symbol=p["symbol"], qty=float(p["qty"]),
            asset_class=p.get("asset_class", "us_equity"),
            avg_cost=float(p.get("avg_entry_price", 0.0)),
            market_value=float(p.get("market_value", 0.0)),
        )
        for p in (ctx.positions_fetcher() or [])
    ]

    conn = connect_writer(ctx.ledger_db)
    try:
        result = submit_order(
            conn=conn, intent=intent, account=account, positions=positions,
            policy=policy, lane=intent_dict.get("lane", "etf_momentum"),
            quote_lane="equity",
            intent_price=float(intent_dict["intent_price"]),
            broker_submit=ctx.broker_submit,
        )
        conn.commit()
    finally:
        conn.close()

    log.info(
        "dispatch: %s %s %.4f %s verdict=%s submitted=%s",
        intent.symbol, intent.side, intent.qty, intent.client_order_id,
        result.risk_verdict, result.submitted,
    )
    return result.submitted


__all__ = [
    "LIVE_STATUSES", "STRATEGY_MODULE",
    "dispatch_all_strategies",
]
