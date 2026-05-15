"""Daemon jobs — pure functions that take a ``DaemonContext`` and run a
single tick of one responsibility.

Every job:
  * opens its own short-lived sqlite connection (single-writer lock is
    acquired by the writer functions themselves),
  * catches and logs unexpected exceptions instead of bubbling — the
    scheduler must survive one bad tick,
  * records a row in ``daemon_heartbeat`` so the dashboard can show
    "last tick at HH:MM:SS".

Wall-clock cadence is set in ``scheduler.py``; this file owns the
*content* of each tick.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from trading_bot.daemon.market_clock import is_equity_rth
from trading_bot.kernel.boot import BootReport, run_boot_checks
from trading_bot.ledger import (
    DEFAULT_LEDGER_PATH, DEFAULT_MIRROR_PATH,
    connect_writer, write_snapshot_batch,
)
from trading_bot.ledger.reconciliation import compute_recon, write_recon_proof
from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Daemon heartbeat — a tiny mutable table so the dashboard can answer
# "is the daemon alive?" without touching every other table.
#
# account_snapshot is also mutable in the sense that we append rows
# continuously; the dashboard reads the latest row for "today's equity"
# and computes intraday P&L by comparing latest vs the first row of the
# session.
# ---------------------------------------------------------------------------

DDL_DAEMON_HEARTBEAT = """
CREATE TABLE IF NOT EXISTS daemon_heartbeat (
    job_name        TEXT PRIMARY KEY,
    last_run_ts     TEXT NOT NULL,
    last_status     TEXT NOT NULL,      -- ok | error | skipped
    last_detail     TEXT,
    last_duration_s REAL
);
"""

DDL_ACCOUNT_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS account_snapshot (
    ledger_seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts     TEXT NOT NULL,
    equity          REAL NOT NULL,
    cash            REAL NOT NULL,
    buying_power    REAL NOT NULL,
    daytrade_count  INTEGER NOT NULL DEFAULT 0,
    pattern_day_trader INTEGER NOT NULL DEFAULT 0,
    broker_status   TEXT
);
CREATE INDEX IF NOT EXISTS idx_account_snapshot_ts ON account_snapshot(snapshot_ts);
"""


def ensure_account_snapshot_table(conn: sqlite3.Connection) -> None:
    for stmt in DDL_ACCOUNT_SNAPSHOT.strip().split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()


def ensure_heartbeat_table(conn: sqlite3.Connection) -> None:
    conn.execute(DDL_DAEMON_HEARTBEAT)
    conn.commit()


def record_heartbeat(
    conn: sqlite3.Connection, *,
    job_name: str, status: str, detail: str = "", duration_s: float = 0.0,
) -> None:
    ensure_heartbeat_table(conn)
    conn.execute(
        """
        INSERT INTO daemon_heartbeat (job_name, last_run_ts, last_status, last_detail, last_duration_s)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(job_name) DO UPDATE SET
            last_run_ts     = excluded.last_run_ts,
            last_status     = excluded.last_status,
            last_detail     = excluded.last_detail,
            last_duration_s = excluded.last_duration_s
        """,
        (job_name, dt.datetime.now(dt.timezone.utc).isoformat(),
         status, detail, duration_s),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Context the scheduler hands every job.
# ---------------------------------------------------------------------------

BrokerCallbackT = Callable[..., dict]
"""``broker.submit_order`` shape (see execution.order_router)."""

PositionsFetcherT = Callable[[], list[dict]]
"""Returns the live broker position vector for reconciliation."""

BarsFetcherT = Callable[..., dict]
"""Returns a bars payload for the watermark writer."""

AccountFetcherT = Callable[[], dict]
"""Returns ``{equity, cash, buying_power, daytrade_count, pattern_day_trader, status}``."""


@dataclass
class DaemonContext:
    ledger_db: Path = field(default_factory=lambda: Path.cwd() / DEFAULT_LEDGER_PATH)
    mirror_db: Path = field(default_factory=lambda: Path.cwd() / DEFAULT_MIRROR_PATH)
    policy_dir: Path = field(default_factory=lambda: DEFAULT_POLICY_DIR)
    broker_submit: Optional[BrokerCallbackT] = None
    positions_fetcher: Optional[PositionsFetcherT] = None
    bars_fetcher: Optional[BarsFetcherT] = None
    account_fetcher: Optional[AccountFetcherT] = None
    universe: tuple[str, ...] = ()
    # Data-driven discovery (Plan v4 §3 — no hardcoded UNIVERSE constants).
    # asset_fetcher(asset_class) -> Sequence[AssetRecord]; usually wired
    # from AlpacaAdapter.list_assets. volume_provider(symbol) -> ADV in USD
    # or None; usually wired from the historical-bars store.
    asset_fetcher: Optional[Callable[[str], object]] = None
    volume_provider: Optional[Callable[[str], Optional[float]]] = None
    # Intel feeds (FRED, EDGAR, CryptoPanic, …) consulted at decision
    # time and snapshotted into ``feature_snapshot.intel_json``. The
    # daemon never reads from feeds on the hot path; only the snapshot
    # writer does. Failure semantics: each feed runs independently
    # (see ``ingest.intel.snapshot_payload``).
    intel_feeds: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Job functions — each is independent, idempotent on retry, and logs its
# own heartbeat.
# ---------------------------------------------------------------------------

def _wrap(job_name: str):
    """Decorator: time + capture exceptions + record heartbeat."""

    def deco(fn):
        def wrapped(ctx: DaemonContext):
            t0 = dt.datetime.now(dt.timezone.utc)
            status, detail = "ok", ""
            try:
                detail = fn(ctx) or ""
            except Exception as e:  # noqa: BLE001 — daemon resilience
                log.exception("job %s failed", job_name)
                status, detail = "error", f"{type(e).__name__}: {e}"
            finally:
                dur = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()
                try:
                    conn = connect_writer(ctx.ledger_db)
                    try:
                        record_heartbeat(
                            conn, job_name=job_name, status=status,
                            detail=detail[:500], duration_s=dur,
                        )
                    finally:
                        conn.close()
                except Exception:
                    log.exception("heartbeat write failed for %s", job_name)
            return status
        wrapped.__name__ = fn.__name__
        return wrapped
    return deco


@_wrap("boot_check")
def job_boot_check(ctx: DaemonContext) -> str:
    report: BootReport = run_boot_checks(
        ledger_db=ctx.ledger_db, mirror_db=ctx.mirror_db,
        policy_dir=ctx.policy_dir,
    )
    if not report.ok:
        # Surface failed checks compactly.
        failed = [c for c in report.checks if c["status"] not in ("ok", "info")]
        raise RuntimeError(
            f"boot_check failed: {json.dumps(failed)[:300]}"
        )
    return f"checks={len(report.checks)} active_kills={len(report.active_kills)}"


@_wrap("position_snapshot")
def job_position_snapshot(ctx: DaemonContext) -> str:
    """Snapshot live positions every 5 min.

    Requires ``positions_fetcher`` to be wired. If not wired, marks the
    job as skipped (daemon still ticks; dashboard shows reason).
    """
    if ctx.positions_fetcher is None:
        return "skipped: positions_fetcher not wired"
    positions = ctx.positions_fetcher()
    if not positions:
        return "ok: 0 positions"
    conn = connect_writer(ctx.ledger_db)
    try:
        write_snapshot_batch(
            conn,
            [
                {
                    "symbol": p["symbol"],
                    "qty": float(p["qty"]),
                    "avg_cost": float(p.get("avg_entry_price", 0.0)),
                    "market_price": float(p.get("market_price", 0.0)),
                    "market_value": float(p.get("market_value", 0.0)),
                    "asset_class": p.get("asset_class", "equity"),
                    "classification": p.get("classification", "unknown"),
                }
                for p in positions
            ],
            source="broker",
        )
        conn.commit()
    finally:
        conn.close()
    return f"ok: {len(positions)} positions"


@_wrap("reconciliation")
def job_reconciliation(ctx: DaemonContext) -> str:
    """Nightly + at-close reconciliation.

    Reads the latest position_snapshot batch as the bot's view and
    compares against the live broker fetcher. compute_recon returns
    (bot_hash, broker_hash, match, diff_json).
    """
    if ctx.positions_fetcher is None:
        return "skipped: positions_fetcher not wired"
    broker_positions = ctx.positions_fetcher()
    conn = connect_writer(ctx.ledger_db)
    try:
        # Latest snapshot batch = bot's view.
        cur = conn.execute(
            "SELECT MAX(snapshot_ts) FROM position_snapshot WHERE source='broker'"
        )
        row = cur.fetchone()
        bot_positions = []
        if row and row[0]:
            cur2 = conn.execute(
                "SELECT symbol, qty, asset_class FROM position_snapshot "
                "WHERE source='broker' AND snapshot_ts=?",
                (row[0],),
            )
            bot_positions = [
                {"symbol": r[0], "qty": r[1], "asset_class": r[2]}
                for r in cur2.fetchall()
            ]
        bot_hash, broker_hash, match, diff = compute_recon(
            bot_positions=bot_positions, broker_positions=broker_positions,
        )
        write_recon_proof(
            conn, recon_window="eod", bot_hash=bot_hash,
            broker_hash=broker_hash, match=match, diff_json=diff,
        )
        conn.commit()
        return f"match={match} bot={bot_hash[:8]} broker={broker_hash[:8]}"
    finally:
        conn.close()


@_wrap("orphan_loop")
def job_orphan_loop(ctx: DaemonContext) -> str:
    from trading_bot.execution.orphan_loop import run_once
    if ctx.broker_submit is None:
        return "skipped: broker not wired"
    # The orphan loop needs broker_lookup, not submit. The Alpaca adapter
    # exposes both; the scheduler passes the lookup via the context's
    # broker_submit attribute when the adapter is installed (see
    # daemon/main.py).
    conn = connect_writer(ctx.ledger_db)
    try:
        # broker_submit doubles as a holder for the adapter; if it is an
        # adapter object with .lookup, use it; otherwise skip.
        adapter = getattr(ctx, "_broker_adapter", None)
        if adapter is None or not hasattr(adapter, "lookup_by_client_order_id"):
            return "skipped: adapter has no lookup_by_client_order_id"
        results = run_once(conn, broker_lookup=adapter.lookup_by_client_order_id)
        return f"recovered={len(results)}"
    finally:
        conn.close()


@_wrap("market_data_ingest")
def job_market_data_ingest(ctx: DaemonContext) -> str:
    """Pull latest bars for the configured universe and update watermarks.

    Outside US equity RTH the job is a no-op — Alpaca bars don't update
    after-hours and we don't want the freshness kill switch firing for
    market hours. The dashboard reflects this as "market closed".
    """
    if ctx.bars_fetcher is None:
        return "skipped: bars_fetcher not wired"
    if not ctx.universe:
        return "skipped: universe is empty"
    if not is_equity_rth():
        return "ok: market closed (RTH gate)"
    from trading_bot.ingest.alpaca_writer import ingest_bars_once
    n = ingest_bars_once(
        ledger_db=ctx.ledger_db,
        symbols=ctx.universe,
        bars_fetcher=ctx.bars_fetcher,
    )
    return f"ok: {n} symbols watermarked"


@_wrap("account_snapshot")
def job_account_snapshot(ctx: DaemonContext) -> str:
    """Pull account equity / cash / buying power and append a row.

    Runs every 5 min during RTH and every 30 min off-hours (the
    scheduler controls cadence). The dashboard reads the latest row for
    "current equity" and the first row of the session for "today's
    intraday P&L".
    """
    if ctx.account_fetcher is None:
        return "skipped: account_fetcher not wired"
    acct = ctx.account_fetcher()
    if not acct:
        return "skipped: account fetch returned empty"
    conn = connect_writer(ctx.ledger_db)
    try:
        ensure_account_snapshot_table(conn)
        conn.execute(
            """
            INSERT INTO account_snapshot
                (snapshot_ts, equity, cash, buying_power,
                 daytrade_count, pattern_day_trader, broker_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dt.datetime.now(dt.timezone.utc).isoformat(),
                float(acct.get("equity", 0.0)),
                float(acct.get("cash", 0.0)),
                float(acct.get("buying_power", 0.0)),
                int(acct.get("daytrade_count", 0) or 0),
                1 if acct.get("pattern_day_trader") else 0,
                str(acct.get("status", "")),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return f"ok: equity=${acct.get('equity', 0):.2f}"


@_wrap("drift_monitor")
def job_drift_monitor(ctx: DaemonContext) -> str:
    """Nightly slippage drift check (Plan v4 §9).

    Reads decision-time intent_price from ``strategy_decision`` for each
    recent fill. Computes realised slippage vs the pessimistic-lens
    modelled mean from ``policy/cost_model.lock``. Per-lane: equity then
    crypto.
    """
    from trading_bot.execution.drift_monitor import compute_drift
    import json
    cost_lock_path = ctx.policy_dir / "cost_model.lock"
    try:
        cost_lock = json.loads(cost_lock_path.read_text())
    except FileNotFoundError:
        return "skipped: cost_model.lock missing"
    equity_modelled_bps = float(cost_lock.get("stocks", {}).get("extra_slippage_bps", 5))
    crypto_modelled_bps = float(cost_lock.get("crypto", {}).get("extra_slippage_bps", 10))

    conn = connect_writer(ctx.ledger_db)
    try:
        def decision_mid_lookup(fill_row: dict) -> float:
            """Look up the decision-time intent_price for a fill's order.

            Falls back to 0.0 if no matching strategy_decision exists,
            which causes the compute_drift function to skip that fill.
            """
            cur = conn.execute(
                "SELECT intent_price FROM strategy_decision "
                "WHERE emitted_client_order_id IN ("
                "    SELECT client_order_id FROM order_master WHERE order_uid=?"
                ") ORDER BY ledger_seq DESC LIMIT 1",
                (fill_row.get("order_uid", ""),),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0

        # Run for each lane that has filled trades.
        reports = []
        for lane, modelled in (("equity", equity_modelled_bps), ("crypto", crypto_modelled_bps)):
            try:
                rep = compute_drift(
                    conn, lane=lane,
                    decision_mid_lookup=decision_mid_lookup,
                    modelled_mean_bps=modelled,
                )
                reports.append((lane, rep))
            except Exception as e:  # noqa: BLE001
                log.warning("drift_monitor lane=%s failed: %s", lane, e)
        if not reports:
            return "ok: no lanes evaluated"
        # Persist + alert per lane that produced any signal. n_trades=0
        # lanes (no fills in window) still emit a row so the operator
        # can prove the monitor ran — without that, an empty ledger
        # could equally mean "no fills" or "drift_monitor crashed".
        from trading_bot.ledger.drift_event import write_event
        from trading_bot.execution.drift_monitor import (
            TOLERANCE_MULTIPLIER_DEFAULT,
        )
        for lane, rep in reports:
            write_event(
                conn, lane=lane, n_trades=rep.n_trades,
                modelled_mean_bps=rep.modelled_mean_bps,
                realised_mean_bps=rep.realised_mean_bps,
                ratio=rep.ratio,
                tolerance_multiplier=TOLERANCE_MULTIPLIER_DEFAULT,
                breach=rep.breach, recommendation=rep.recommendation,
            )
        conn.commit()
        # Fire one alert per breaching lane. The notifier deduplicates
        # by (lane, UTC-date) so persistent breaches don't spam.
        try:
            from trading_bot.obs.notifier import send_drift_alert
            for lane, rep in reports:
                if rep.breach:
                    send_drift_alert(
                        lane=lane, n_trades=rep.n_trades,
                        modelled_mean_bps=rep.modelled_mean_bps,
                        realised_mean_bps=rep.realised_mean_bps,
                        ratio=rep.ratio,
                        recommendation=rep.recommendation,
                    )
        except Exception as e:  # noqa: BLE001 — alert path must not crash drift job
            log.warning("drift_monitor alert failed: %s", e)
        parts = [
            f"{lane}:n={r.n_trades},ratio={r.ratio:.2f}"
            + (f",DEMOTE" if r.breach else "")
            for lane, r in reports
        ]
        return "ok: " + " ".join(parts)
    finally:
        conn.close()


@_wrap("strategy_runner")
def job_strategy_runner(ctx: DaemonContext) -> str:
    """Tick every enabled strategy.

    For each strategy with status in {tiny_paper, scaled_paper, live}:
      * evaluate the signal as-of today (or skip if not a rebalance day)
      * convert target weights to OrderIntents
      * submit each intent via execution.order_router

    Strategies at ``research_only`` or ``shadow`` are NOT ticked — they
    are paper-observed via the backtest harness instead. (Plan v4 §7
    lane state transitions.)

    Skips cleanly if no broker is wired (--no-broker mode).
    """
    if ctx.positions_fetcher is None or ctx.account_fetcher is None:
        return "skipped: broker not wired"
    if ctx.broker_submit is None:
        return "skipped: broker_submit not wired"

    import os
    if os.environ.get("TRADING_BOT_ENABLE_STRATEGY_RUNNER", "").lower() not in {"1", "true", "yes"}:
        return "skipped: TRADING_BOT_ENABLE_STRATEGY_RUNNER not set"

    from trading_bot.daemon.strategy_dispatch import dispatch_all_strategies
    out = dispatch_all_strategies(ctx)
    return f"ok: {out}"


@_wrap("mutation_cycle")
def job_mutation_cycle(ctx: DaemonContext) -> str:
    # Nightly cadence under v4 Phase C — runs across all registered v3
    # families. Skips until the operator has opted in with
    # TRADING_BOT_ENABLE_MUTATION_CYCLE=1 and (optionally) configured a
    # persona-runner command via TRADING_BOT_MUTATION_PERSONA_CMD.
    import os
    if os.environ.get("TRADING_BOT_ENABLE_MUTATION_CYCLE", "").lower() not in {"1", "true", "yes"}:
        return "skipped: TRADING_BOT_ENABLE_MUTATION_CYCLE not set"
    try:
        from trading_bot.research.mutation_runner import run_nightly_cycle
    except ImportError:
        return "skipped: research.mutation_runner not available"
    out = run_nightly_cycle(ctx.ledger_db, policy_dir=ctx.policy_dir)
    return f"ok: {out}"


# ---------------------------------------------------------------------------
# v4 Phase A — universe audit + regime monitor + drift postmortem chain.
# ---------------------------------------------------------------------------

@_wrap("universe_audit")
def job_universe_audit(ctx: DaemonContext) -> str:
    """Weekly universe audit (Sundays 22:00 ET via scheduler).

    For each strategy registered with status >= shadow, recompute the
    discovered universe, diff against last week, persist a
    ``universe_audit_event`` row, and on breach invoke the
    universe_audit_analyst persona via the postmortem driver.
    """
    try:
        from trading_bot.research.universe_discovery import (
            compute_audit, discover,
        )
        from trading_bot.ledger.universe_audit_event import (
            latest_for_strategy, write_event as write_audit,
        )
        from trading_bot.obs.drift_postmortem import write_memo
    except ImportError as e:
        return f"skipped: import error: {e}"

    # Hard-coded set of v3 strategies that opt into universe audit.
    # When a new v3 family is registered, add it here + its policy path.
    strategies = [
        ("ETF_MOMENTUM_v3", ctx.policy_dir / "etf_universe_v1.json",
         ("SPY", "QQQ", "IWM")),
        ("CRYPTO_MOMENTUM_v3", ctx.policy_dir / "crypto_universe_v1.json",
         ("BTC/USD", "ETH/USD")),
    ]

    conn = connect_writer(ctx.ledger_db)
    n_breaches = 0
    n_audited = 0
    try:
        for strategy_id, policy_path, fallback in strategies:
            try:
                ru = discover(
                    strategy_id=strategy_id, policy_path=policy_path,
                    asset_fetcher=getattr(ctx, "asset_fetcher", None),
                    volume_provider=getattr(ctx, "volume_provider", None),
                    fallback_symbols=fallback,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("universe_audit: discover failed for %s: %s",
                            strategy_id, e)
                continue
            prev = latest_for_strategy(conn, strategy_id)
            prev_members = prev.get("members") if prev else ()
            audit = compute_audit(
                strategy_id=strategy_id,
                current_members=ru.symbols,
                previous_members=prev_members,
            )
            seq = write_audit(
                conn,
                strategy_id=strategy_id,
                members=audit["members"], additions=audit["additions"],
                removals=audit["removals"], turnover_pct=audit["turnover_pct"],
                breach=audit["breach"],
            )
            n_audited += 1
            if audit["breach"]:
                n_breaches += 1
                # Best-effort Claude memo; failure does not crash the job.
                try:
                    write_memo(
                        conn,
                        source_event_type="universe_audit_event",
                        source_ledger_seq=seq,
                        event_payload=audit,
                        persona_id="universe_audit_analyst",
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("universe_audit memo failed: %s", e)
        conn.commit()
    finally:
        conn.close()
    return f"ok: audited={n_audited} breaches={n_breaches}"


@_wrap("regime_monitor")
def job_regime_monitor(ctx: DaemonContext) -> str:
    """Regime classifier tick.

    Reads current signals (VIX, drawdown, fear/greed where available),
    classifies each asset class, and writes a ``regime_event`` row when
    the regime changes from the prior tick. Manual override always wins.
    Off-hours during normal regime: cheap; the scheduler runs this every
    30 min RTH and every 4 h overnight.
    """
    try:
        from trading_bot.risk.regime_classifier import (
            RegimeSignals, classify,
        )
        from trading_bot.risk.manual_regime_override import (
            applies_to, load as load_override,
        )
        from trading_bot.ledger.regime_event import (
            current_regime, write_event as write_regime,
        )
    except ImportError as e:
        return f"skipped: import error: {e}"

    override = load_override(ctx.policy_dir)
    conn = connect_writer(ctx.ledger_db)
    try:
        out_parts = []
        for asset_class in ("stocks", "crypto", "options"):
            prior = current_regime(conn, asset_class)

            if override is not None and applies_to(override, asset_class):
                new_regime = override.forced_regime or prior
                source = "manual"
                triggering = {"override_reason": override.reason_md}
            else:
                # Real-world signal fetching is operator-injected; the
                # daemon ships a `regime_signal_provider` callable on ctx
                # when configured. The Phase-A default is "no signals
                # available" → classifier returns normal, which is fine
                # for paper mode without a Yahoo / VIX feed.
                provider = getattr(ctx, "regime_signal_provider", None)
                if provider is None:
                    signals = RegimeSignals()
                else:
                    try:
                        signals = provider(asset_class)
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "regime_signal_provider failed for %s: %s",
                            asset_class, e,
                        )
                        signals = RegimeSignals()
                verdict = classify(asset_class=asset_class, signals=signals)
                new_regime = verdict.regime
                source = verdict.source
                triggering = verdict.triggering_signals

            if new_regime != prior:
                seq = write_regime(
                    conn,
                    asset_class=asset_class,
                    prior_regime=prior, new_regime=new_regime,
                    source=source,
                    trigger_signals=triggering,
                    mandated_actions=[],
                )
                # On crisis-entry, fire a postmortem memo (best-effort).
                if new_regime == "crisis":
                    try:
                        from trading_bot.obs.drift_postmortem import write_memo
                        write_memo(
                            conn,
                            source_event_type="regime_event",
                            source_ledger_seq=seq,
                            event_payload={
                                "asset_class": asset_class,
                                "prior_regime": prior,
                                "new_regime": new_regime,
                                "source": source,
                                "triggering": triggering,
                            },
                            persona_id="regime_analyst",
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("regime crisis memo failed: %s", e)
                out_parts.append(f"{asset_class}:{prior}->{new_regime}")
            else:
                out_parts.append(f"{asset_class}:{prior}=")
        conn.commit()
        return "ok: " + " ".join(out_parts)
    finally:
        conn.close()


@_wrap("intel_refresh")
def job_intel_refresh(ctx: DaemonContext) -> str:
    """Refresh cache-backed intel feeds. Runs every 6 hours per
    scheduler. Per-feed failures are non-fatal (logged + reported in
    the summary string)."""
    try:
        import sys
        sys.path.insert(0, str((ctx.policy_dir.parent / "tools").resolve()))
        from refresh_intel_caches import refresh_all
    except Exception as e:  # noqa: BLE001
        return f"skipped: import error {e}"
    results = refresh_all()
    n_ok = sum(1 for v in results.values() if v == "ok")
    n_fail = sum(1 for v in results.values() if v.startswith("fail:"))
    return f"ok: {n_ok} refreshed, {n_fail} failed"


@_wrap("source_scout")
def job_source_scout(ctx: DaemonContext) -> str:
    """Research-bot source scout — every 6h (Phase D).

    Off by default; opt in with TRADING_BOT_ENABLE_RESEARCH_BOT=1.
    """
    import os
    if os.environ.get("TRADING_BOT_ENABLE_RESEARCH_BOT", "").lower() not in {"1", "true", "yes"}:
        return "skipped: TRADING_BOT_ENABLE_RESEARCH_BOT not set"
    try:
        from trading_bot.research.research_bot import run_source_scouts
    except ImportError as e:
        return f"skipped: {e}"
    out = run_source_scouts(ctx.ledger_db, policy_dir=ctx.policy_dir)
    return f"ok: {out}"


@_wrap("strategy_intake")
def job_strategy_intake(ctx: DaemonContext) -> str:
    """Research-bot intake / codegen / paper-validation pipeline (Phase D)."""
    import os
    if os.environ.get("TRADING_BOT_ENABLE_RESEARCH_BOT", "").lower() not in {"1", "true", "yes"}:
        return "skipped: TRADING_BOT_ENABLE_RESEARCH_BOT not set"
    try:
        from trading_bot.research.research_bot import run_intake_pipeline
    except ImportError as e:
        return f"skipped: {e}"
    out = run_intake_pipeline(ctx.ledger_db, policy_dir=ctx.policy_dir)
    return f"ok: {out}"


@_wrap("mutation_review")
def job_mutation_review(ctx: DaemonContext) -> str:
    """Weekly Claude memo on last week's mutation outcomes (Phase C)."""
    try:
        from trading_bot.research.mutation_runner import run_weekly_review
    except ImportError as e:
        return f"skipped: {e}"
    out = run_weekly_review(ctx.ledger_db)
    return f"ok: {out}"


@_wrap("search_space_proposal")
def job_search_space_proposal(ctx: DaemonContext) -> str:
    """Monthly Claude memo proposing search-space additions (Phase C)."""
    try:
        from trading_bot.research.mutation_runner import run_monthly_expansion
    except ImportError as e:
        return f"skipped: {e}"
    out = run_monthly_expansion(ctx.ledger_db)
    return f"ok: {out}"


__all__ = [
    "DDL_ACCOUNT_SNAPSHOT",
    "DDL_DAEMON_HEARTBEAT",
    "DaemonContext",
    "ensure_account_snapshot_table",
    "ensure_heartbeat_table",
    "job_account_snapshot",
    "job_boot_check",
    "job_drift_monitor",
    "job_market_data_ingest",
    "job_mutation_cycle",
    "job_intel_refresh",
    "job_mutation_review",
    "job_orphan_loop",
    "job_position_snapshot",
    "job_reconciliation",
    "job_regime_monitor",
    "job_search_space_proposal",
    "job_source_scout",
    "job_strategy_intake",
    "job_strategy_runner",
    "job_universe_audit",
    "record_heartbeat",
]
