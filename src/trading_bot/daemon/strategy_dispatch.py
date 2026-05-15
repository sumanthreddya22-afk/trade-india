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
import hashlib
import inspect
import json
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
    "DUAL_MOMENTUM_v1": "trading_bot.strategies.dual_momentum_v1",
    "CRYPTO_MOMENTUM_v1": "trading_bot.strategies.crypto_momentum_v1",
    "SPY_WHEEL_v1": "trading_bot.strategies.spy_wheel_v1",
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

    # Anchor "today" in US/Eastern — the market clock the daemon
    # trades against. Wall-clock midnight in non-ET locales would
    # otherwise advance the date 4-7 hours early.
    from trading_bot.daemon.market_clock import NY_TZ
    today = dt.datetime.now(dt.timezone.utc).astimezone(NY_TZ).date()
    # Holiday gate — never fire equity strategies on a US market
    # closure. Crypto-only strategies opt out by setting
    # ``RUNS_ON_NON_TRADING_DAYS = True`` at module level.
    from trading_bot.daemon.market_calendar import (
        is_us_equity_trading_day,
    )
    runner = importlib.import_module(f"{module_path}.runner")
    runs_24_7 = bool(getattr(runner, "RUNS_ON_NON_TRADING_DAYS", False))
    if not runs_24_7 and not is_us_equity_trading_day(today):
        return {"action": "skipped_market_closed",
                "today": today.isoformat()}
    if not runner.should_rebalance_today(today, last_date):
        return {"action": "skipped_cadence", "last_date": str(last_date)}

    decision = runner.evaluate_strategy(
        decision_date=today,
        **_runner_extras(runner.evaluate_strategy, ctx),
    )
    # Persist a feature_snapshot row when the runner exposed a
    # universe_payload — anchors backtest replay to the same inputs
    # the live decision saw. Runners that don't (yet) report a
    # universe simply skip this write.
    snapshot_id = _maybe_write_feature_snapshot(ctx, strategy_id, decision)
    if not decision.intents:
        # The strategy evaluated but emitted no orders (e.g. wheel
        # ran out of options BP, or already at target weights).
        # Log a ``risk_decision="skip"`` row so the operator sees the
        # skip in the ledger instead of having to grep the daemon log.
        _record_skip(ctx, strategy_id, strategy_ver, decision, today,
                     feature_snapshot_id=snapshot_id)
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
    acct = ctx.account_fetcher() or {}
    equity = float(acct.get("equity", 0.0))
    # Read session-start equity from ``account_snapshot`` so the
    # account-level kill switches (daily DD, intraday floor) actually
    # gate intraday losses. Before the first snapshot of the session
    # this degrades to ``equity`` (same as the prior behaviour).
    from trading_bot.daemon.session_state import session_start_equity
    _conn = sqlite3.connect(ctx.ledger_db)
    try:
        session_start = session_start_equity(
            _conn, fallback_equity=equity,
        )
    finally:
        _conn.close()
    account = AccountState(
        equity=equity,
        cash=float(acct.get("cash", 0.0)),
        equity_at_session_start=session_start,
        day_trade_count=int(acct.get("daytrade_count", 0) or 0),
        buying_power=float(acct.get("buying_power", 0.0)),
    )
    positions = [
        Position(
            symbol=p["symbol"], qty=float(p["qty"]),
            asset_class=p.get("asset_class", "us_equity"),
            market_value=float(p.get("market_value", 0.0)),
            classification=p.get("classification", "unknown"),
        )
        for p in (ctx.positions_fetcher() or [])
    ]

    # Per-order risk cap (2% equity default) requires a stop_loss_price
    # to translate notional → at-risk dollars. Without one the kernel
    # treats the full notional as at-risk and most rebalance orders
    # would halt. We provide a conservative default per asset class —
    # operator can override via intent_dict['stop_loss_price'].
    intent_price = float(intent_dict["intent_price"])
    asset_class = intent_dict.get("asset_class", "us_equity").lower()
    side_lower = intent_dict["side"].lower()
    explicit_stop = intent_dict.get("stop_loss_price")
    if explicit_stop is not None:
        stop_loss_price = float(explicit_stop)
    elif side_lower == "buy":
        # Long entry: stop below entry. Crypto more volatile → wider stop.
        stop_pct = 0.20 if asset_class == "crypto" else 0.10
        stop_loss_price = intent_price * (1.0 - stop_pct)
    else:
        # Short entry / sell-to-close: stop above (matters less for sells
        # which the kernel mostly passes through).
        stop_loss_price = intent_price * 1.10
    # Map asset_class → quote_lane for the freshness check.
    quote_lane = "crypto" if asset_class == "crypto" else (
        "option" if asset_class in ("us_option", "option") else "equity"
    )

    conn = connect_writer(ctx.ledger_db)
    try:
        result = submit_order(
            conn=conn, intent=intent, account=account, positions=positions,
            policy=policy, lane=intent_dict.get("lane", "etf_momentum"),
            quote_lane=quote_lane,
            intent_price=intent_price,
            stop_loss_price=stop_loss_price,
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


def _runner_extras(evaluate_fn, ctx: "DaemonContext") -> dict:
    """Map DaemonContext providers onto a runner's keyword-only params.

    Different strategy runners accept different shapes today —
    dual_momentum_v1 takes ``asset_fetcher`` + ``volume_provider``,
    the older runners take only ``positions_fetcher`` +
    ``account_fetcher``. Inspect the runner's signature so we never
    pass a kwarg it doesn't accept (which would TypeError) and never
    drop one it does (which would silently fall back to defaults).
    """
    params = inspect.signature(evaluate_fn).parameters
    available = {
        "positions_fetcher": ctx.positions_fetcher,
        "account_fetcher": ctx.account_fetcher,
        "asset_fetcher": getattr(ctx, "asset_fetcher", None),
        "volume_provider": getattr(ctx, "volume_provider", None),
    }
    return {name: val for name, val in available.items() if name in params}


def _collect_intel(ctx: "DaemonContext", decision_date) -> dict:
    """Run every configured intel feed and return a single payload
    suitable for ``feature_snapshot.intel_json``. Returns ``{}`` when
    no feeds are configured (current default for fresh worktrees and
    unit tests). Feed-level failures are isolated — see
    ``ingest.intel.snapshot_payload`` for fail-closed semantics."""
    feeds = list(getattr(ctx, "intel_feeds", None) or [])
    if not feeds:
        return {}
    import datetime as _dt
    when = decision_date or _dt.datetime.now(_dt.timezone.utc).date()
    try:
        from trading_bot.ingest.intel import snapshot_payload
        return snapshot_payload(feeds, when)
    except Exception as e:  # noqa: BLE001 — intel must never crash the dispatch
        log.warning("intel snapshot failed: %s", e)
        return {"_error": f"{type(e).__name__}: {e}"}


def _maybe_write_feature_snapshot(
    ctx: "DaemonContext", strategy_id: str, decision: object,
) -> str:
    """Append a feature_snapshot row if the runner exposed a
    universe_payload. Returns the snapshot_id (empty string when
    nothing was written) so the strategy_decision row can reference
    it later.

    snapshot_id is content-addressed over (universe_payload, intel)
    — replaying the same inputs yields the same id, so the
    idempotent insert collapses duplicate decisions. ``intel`` is
    {} today; once FRED / EDGAR feeds are wired in, the daemon will
    populate it here and the hash changes accordingly.
    """
    payload = getattr(decision, "universe_payload", None) or {}
    if not payload:
        return ""
    intel = _collect_intel(ctx, getattr(decision, "decision_date", None))
    body = json.dumps(
        {"universe": payload, "intel": intel},
        sort_keys=True, separators=(",", ":"),
        default=str,
    )
    snapshot_id = f"feat:{hashlib.sha256(body.encode()).hexdigest()[:24]}"
    from trading_bot.ledger import connect_writer
    from trading_bot.ledger.feature_snapshot import insert_or_get
    conn = connect_writer(ctx.ledger_db)
    try:
        insert_or_get(
            conn, snapshot_id=snapshot_id, strategy_id=strategy_id,
            universe=payload, intel=intel,
        )
        conn.commit()
    finally:
        conn.close()
    return snapshot_id


def _record_skip(
    ctx: "DaemonContext",
    strategy_id: str,
    strategy_ver: int,
    decision: object,
    decision_date: dt.date,
    *,
    feature_snapshot_id: str = "",
) -> None:
    """Append a ``risk_decision="skip"`` row for a strategy that
    evaluated but produced no orders.

    Without this the daemon-log message ``no_intents`` is the only
    trace; the wheel can skip for weeks with nothing in the ledger to
    show the operator why. Skip rows carry a synthetic intent_json
    capturing the (empty) target_weights and any rationale the signal
    exposed so a postmortem can reconstruct the choice.
    """
    from trading_bot.ledger import connect_writer
    from trading_bot.ledger.strategy_decision import write_decision
    from trading_bot.risk import load_policy

    snapshot_ref = feature_snapshot_id or "daemon-skip"
    target_weights = getattr(decision, "target_weights", {}) or {}
    signal = getattr(decision, "signal", None)
    rationale = getattr(signal, "rationale", "") if signal is not None else ""
    synthetic_intent = {
        "action": "skip",
        "decision_date": decision_date.isoformat(),
        "target_weights": dict(target_weights),
        "rationale": rationale,
    }
    try:
        policy = load_policy()
        policy_hash = getattr(policy, "combined_hash", "")
    except Exception:  # noqa: BLE001
        policy_hash = ""
    conn = connect_writer(ctx.ledger_db)
    try:
        write_decision(
            conn,
            strategy_id=strategy_id,
            strategy_ver=strategy_ver,
            code_hash="daemon-skip",
            config_hash="daemon-skip",
            policy_hash=policy_hash or "daemon-skip",
            feature_snapshot_id=snapshot_ref,
            intent=synthetic_intent,
            risk_decision="skip",
            risk_reason=rationale or "no actionable intents",
            emitted_client_order_id=None,
        )
        conn.commit()
    finally:
        conn.close()


__all__ = [
    "LIVE_STATUSES", "STRATEGY_MODULE",
    "dispatch_all_strategies",
]
