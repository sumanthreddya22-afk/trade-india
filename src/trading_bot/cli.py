from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import os

import click

from trading_bot.shared.alpaca_client import AlpacaClient, AssetClass, OrderRequest, OrderSide
from trading_bot.shared.config import Settings, load_config
from trading_bot.email_sender import EmailSender
from trading_bot.email_log import send_logged
from trading_bot.evolution import (
    append_evolution_log,
    apply_proposals,
    evaluate_performance,
    load_params,
    propose_rule_changes,
    save_params,
)
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.intelligence import IntelligenceAggregator, get_macro_snapshot
from trading_bot.last_scan import write_last_scan
from trading_bot.market_data import MarketDataClient
from trading_bot.orchestrator import ScanResult, TradeOrchestrator, load_ranked_watchlist
from trading_bot.pnl_state import PnlStateBuilder
from trading_bot.portfolio_monitor import (
    diff_snapshots,
    has_alerts,
    load_snapshot,
    save_snapshot,
    take_snapshot,
)
from trading_bot.reconciliation import ClosedTradeStore, Reconciler
from trading_bot.regime import detect_regime
from trading_bot.reports import (
    build_open_positions_email_html,
    build_vip_alert_email_html,
    open_positions_email_subject,
)
from trading_bot.email_digest import build_daily_digest_email, DigestContext, TradeRow as DigestTradeRow
from trading_bot.shared.risk_manager import RiskManager, RiskState
from trading_bot.state import WatchlistEntry, load_watchlist
from trading_bot.trade_journal import TradeJournal
from trading_bot.screener import build_stage1_shortlist, run_stage2, write_opportunities_snapshot
from trading_bot.strategy_lanes import BreakoutLane, MeanReversionLane, MomentumLane
from trading_bot.universe import (
    build_universe_from_grouped,
    build_universe_from_seed_list,
    write_universe_snapshot,
)

CONFIG_PATH = Path("strategy/config.yaml")
WATCHLIST_PATH = Path("strategy/watchlist.yaml")
OPPORTUNITIES_PATH = Path("strategy/opportunities.md")
RULES_PATH = Path("strategy/rules.md")
PARAMS_PATH = Path("strategy/params.yaml")
CLOSED_DB_PATH = Path("data/closed_trades.db")
SNAPSHOT_PATH = Path("data/portfolio_snapshot.json")
STATE_DB_PATH = Path("data/state.db")
RESTRICTED_LIST_PATH = Path("strategy/restricted_list.yaml")


def _build_orchestrator(
    *, cfg, market, alpaca, journal, regime: str, settings,
    state_builder=None,
):
    """Construct a TradeOrchestrator wired with the W1+W2 surface area.

    Centralised so every CLI command + daemon job gets the same
    decision-persistence + compliance behaviour automatically. Without this,
    individual call sites silently lose the new gates.
    """
    from trading_bot.decisions_store import DecisionStore
    from trading_bot.orchestrator import TradeOrchestrator
    from trading_bot.state_db import get_engine
    # Phase 5.5/5.6 — pass the state.db engine so the unblock committee
    # can persist debate rows. Hook is opt-in via wheel.unblock_debate_enabled
    # (same flag controls wheel + orchestrator hooks for now).
    unblock_enabled = bool(getattr(getattr(cfg, "wheel", None),
                                   "unblock_debate_enabled", False))
    state_engine = get_engine(STATE_DB_PATH) if unblock_enabled else None
    # Phase 6 — pre-trade entry debate. Reads its enabled-flag from
    # ``cfg.strategy.entry_debate_enabled``. Always wire the engine so a
    # YAML toggle alone is enough to flip the gate on without code change.
    entry_debate_enabled = bool(getattr(cfg.strategy, "entry_debate_enabled", False))
    entry_debate_daily_cap = int(getattr(cfg.strategy, "entry_debate_daily_cap", 50))
    intel_threshold = float(getattr(
        cfg.strategy, "intel_score_regime_override_threshold", 5.0,
    ))
    # The intel pool + entry-debate audit table both live in state.db.
    # Reuse the same engine the unblock_debate already wires.
    state_engine_full = state_engine if state_engine is not None else get_engine(STATE_DB_PATH)
    return TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
        state_builder=state_builder,
        decision_store=DecisionStore(STATE_DB_PATH),
        restricted_list_path=RESTRICTED_LIST_PATH,
        approved_venue_url=settings.alpaca_base_url,
        unblock_debate_enabled=unblock_enabled,
        unblock_debate_engine=state_engine,
        entry_debate_enabled=entry_debate_enabled,
        entry_debate_engine=state_engine_full,
        entry_debate_daily_cap=entry_debate_daily_cap,
        intel_score_regime_override_threshold=intel_threshold,
        intel_lookup_engine=state_engine_full,
    )

ACTIVE_UNIVERSE_TOP_N_STOCKS = int(os.environ.get("TRADING_BOT_SCAN_TOP_N", "100"))
# Bucket B: opportunities.md is rebuilt at 07:30 + 12:00 ET. If it's older
# than this many hours we treat it as stale, fire an alert, and fall back
# to CORE_LIQUID_TICKERS (250 names) instead of trusting yesterday's signal.
OPPORTUNITIES_MAX_AGE_HOURS = float(os.environ.get("TRADING_BOT_OPPORTUNITIES_MAX_AGE_HOURS", "12"))


def _is_usd_crypto(symbol: str) -> bool:
    """Filter out wrapped/cross-quoted crypto pairs (BTC/USDC, LINK/BTC, etc.)
    so we don't double-count the same underlying asset."""
    return symbol.endswith("/USD")


CRYPTO_BLOCKLIST_PATH = Path("strategy/crypto_blocklist.yaml")


def _load_yaml_symbol_set(path: Path) -> set[str]:
    """Read a {symbols: [...]} YAML and return uppercased symbol set.
    Returns empty set if file is missing or malformed."""
    try:
        if not path.exists():
            return set()
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
        return {str(s).upper() for s in (data.get("symbols") or [])}
    except Exception:
        return set()


def _load_crypto_universe():
    """Crypto universe.

    Source preference (highest → lowest):
      1. intel_candidates (continuous internet-driven, asset_class='crypto')
         intersected with Alpaca's tradable list. The pool surfaces names
         that just made news; we still gate on "is it tradable on Alpaca".
      2. Auto-discover from Alpaca's tradable list (legacy behavior — every
         USD-quoted non-stable pair).
    Failure of layer 1 transparently falls through to layer 2.
    """
    from trading_bot.shared.alpaca_client import AlpacaClient
    from trading_bot.crypto_universe import discover_crypto_universe
    try:
        ac = AlpacaClient(Settings())
    except Exception:
        return []
    blocklist = _load_yaml_symbol_set(CRYPTO_BLOCKLIST_PATH)
    discovered = discover_crypto_universe(ac, blocklist=blocklist)

    intel_crypto = _load_intel_pool_crypto()
    if intel_crypto:
        # Intersect with currently tradable, preserve intel score order.
        tradable = {e.symbol for e in discovered}
        ranked = [e for e in intel_crypto if e.symbol in tradable]
        if ranked:
            return ranked
    return discovered


def _load_intel_pool_stocks():
    """Read top stock candidates from intel_candidates. Returns
    WatchlistEntry list ordered by score. Empty list when pool stale or
    missing — caller falls through to existing screener.
    """
    try:
        from trading_bot.intel import pool as intel_pool
        from trading_bot.state import WatchlistEntry
        from trading_bot.state_db import get_engine
        engine = get_engine(STATE_DB_PATH)
        if not intel_pool.is_pool_fresh(engine):
            return []
        entries = intel_pool.top_for_asset_class(
            engine, asset_class="stock", n=ACTIVE_UNIVERSE_TOP_N_STOCKS,
        )
        return [
            WatchlistEntry(
                symbol=e.symbol, asset_class="us_equity",
                notes=f"intel:{e.score:.2f} {e.top_reason[:50]}",
            )
            for e in entries
        ]
    except Exception:
        return []


def _load_intel_pool_crypto():
    """Read top crypto candidates from intel_candidates. Returns
    WatchlistEntry list ordered by score. Empty list on miss.
    """
    try:
        from trading_bot.intel import pool as intel_pool
        from trading_bot.state import WatchlistEntry
        from trading_bot.state_db import get_engine
        engine = get_engine(STATE_DB_PATH)
        if not intel_pool.is_pool_fresh(engine):
            return []
        entries = intel_pool.top_for_asset_class(
            engine, asset_class="crypto", n=20,
        )
        return [
            WatchlistEntry(
                symbol=e.symbol, asset_class="crypto",
                notes=f"intel:{e.score:.2f}",
            )
            for e in entries
        ]
    except Exception:
        return []


def _alert_universe_fallback(*, kind: str, detail: str) -> None:
    """Bucket B: fire an alert when the equity universe degrades to a fallback.

    Best-effort — alerting must never block universe construction. Uses the
    ``daemon_critical`` AlertEvent kind since universe collapse is a first-class
    operational issue.
    """
    try:
        import datetime as _dt_alert
        from trading_bot.alerts import AlertEvent, queue_alert
        queue_alert(AlertEvent(
            kind="daemon_critical",
            severity="warn",
            title=f"Universe fallback: {kind}",
            detail_html=f"<p>{detail}</p>",
            fired_at=_dt_alert.datetime.now(_dt_alert.timezone.utc),
            dedup_key=f"universe_fallback:{kind}",
        ))
    except Exception:
        pass


def _seed_equity_fallback() -> list[WatchlistEntry]:
    """Bucket B: 250-name `CORE_LIQUID_TICKERS` instead of the 7-name
    watchlist.yaml. Replaces the pre-Bucket-B silent collapse to SPY/QQQ/AAPL/
    MSFT/AMD when opportunities.md is missing or stale.

    Tickers are intersected with Alpaca's currently tradable equity list so
    delisted names drop silently. Returns equities only — crypto is handled
    by the dedicated crypto-universe loader.
    """
    try:
        from trading_bot.shared.alpaca_client import AlpacaClient
        from trading_bot.universe import CORE_LIQUID_TICKERS
        ac = AlpacaClient(Settings())
        tradable = {a.symbol for a in ac.get_active_assets("us_equity")}
    except Exception:
        # Last-resort: fall back to whatever symbols we hardcoded as core,
        # without an Alpaca check. Better than a 7-name fallback.
        from trading_bot.universe import CORE_LIQUID_TICKERS
        tradable = set(CORE_LIQUID_TICKERS)
    out: list[WatchlistEntry] = []
    for sym in CORE_LIQUID_TICKERS:
        if sym in tradable:
            out.append(WatchlistEntry(symbol=sym, asset_class="us_equity", notes="seed_fallback"))
    return out


def _load_active_universe(*, crypto_only: bool = False):
    """Active trading universe.

      stocks = top-N ranked from strategy/opportunities.md (screener output;
               source: market-wide Polygon grouped daily). Default cap 100,
               configurable via TRADING_BOT_SCAN_TOP_N. Stage-1 shortlist
               rows are NOT auto-traded (Bucket B fix).
      crypto = auto-discovered from Alpaca's tradable asset list
               (USD-quoted, not stablecoin, not in crypto_blocklist.yaml)

    Bucket B: when ``opportunities.md`` is missing or older than
    ``OPPORTUNITIES_MAX_AGE_HOURS`` (default 12h), the equity lane falls back
    to the 250-name ``CORE_LIQUID_TICKERS`` seed list (NOT the 7-name
    ``strategy/watchlist.yaml``) and fires a ``daemon_critical`` alert.
    """
    from trading_bot.orchestrator import opportunities_age_hours

    age = opportunities_age_hours(OPPORTUNITIES_PATH)
    is_stale = age is None or age > OPPORTUNITIES_MAX_AGE_HOURS
    ranked = load_ranked_watchlist(OPPORTUNITIES_PATH) if not is_stale else []

    # Source preference (highest → lowest) for stocks:
    #   1. intel_candidates pool (continuous, internet-driven) — when fresh
    #   2. opportunities.md (technical screener) — when fresh
    #   3. CORE_LIQUID_TICKERS seed list (cold-start safety net)
    # Cold-start fallback chain stays intact; the intel pool just becomes
    # the FIRST consulted source. ``intel_pool_stocks`` returns an empty
    # list whenever the pool is stale or missing — caller transparently
    # falls through to the existing logic below.
    intel_stock_entries = _load_intel_pool_stocks()

    if is_stale:
        _alert_universe_fallback(
            kind="opportunities_stale",
            detail=(
                f"opportunities.md missing or stale (age={age}h, "
                f"budget={OPPORTUNITIES_MAX_AGE_HOURS}h). Falling back to "
                f"CORE_LIQUID_TICKERS ({250} seed names). "
                "Verify premarket_rank @ 07:30 ET ran today."
            ),
        )

    crypto_universe = _load_crypto_universe()
    # Fall back to opportunities.md ranked crypto (then watchlist.yaml) if
    # Alpaca crypto discovery fails — never leave the bot without a universe.
    if not crypto_universe:
        ranked_crypto = [
            e for e in ranked if e.asset_class == "crypto" and _is_usd_crypto(e.symbol)
        ]
        if ranked_crypto:
            crypto_universe = ranked_crypto
        else:
            try:
                fallback = load_watchlist(WATCHLIST_PATH)
            except Exception:
                fallback = []
            crypto_universe = [
                e for e in fallback if e.asset_class == "crypto" and _is_usd_crypto(e.symbol)
            ]
            if crypto_universe:
                _alert_universe_fallback(
                    kind="crypto_watchlist_fallback",
                    detail="Alpaca crypto discovery + opportunities.md both empty; "
                           "using watchlist.yaml crypto.",
                )

    if crypto_only:
        seen: set[str] = set()
        out: list = []
        for e in crypto_universe:
            if e.symbol in seen:
                continue
            seen.add(e.symbol)
            out.append(e)
        return out

    # Stocks source order:
    #   1. intel_candidates pool (continuous internet-driven)
    #   2. opportunities.md (technical screener output)
    #   3. CORE_LIQUID_TICKERS (cold-start safety net)
    # Each stage falls through transparently when its source is empty.
    if intel_stock_entries:
        stocks = intel_stock_entries[:ACTIVE_UNIVERSE_TOP_N_STOCKS]
    else:
        stocks_ranked = [e for e in ranked if e.asset_class != "crypto"][:ACTIVE_UNIVERSE_TOP_N_STOCKS]
        if stocks_ranked:
            stocks = stocks_ranked
        else:
            if not is_stale:
                # File was fresh but stage-2 had zero endorsements — that's a
                # "low-conviction day" signal. Fall back to seed list rather than
                # produce zero universe so crypto-only behaviour stays consistent.
                _alert_universe_fallback(
                    kind="stage2_empty",
                    detail="opportunities.md is fresh but has zero stage-2 endorsed "
                           "candidates; using CORE_LIQUID_TICKERS seed for equity scan.",
                )
            stocks = _seed_equity_fallback()

    seen = set()
    out = []
    for e in stocks + crypto_universe:
        if e.symbol in seen:
            continue
        seen.add(e.symbol)
        out.append(e)
    return out


def _live_regime(market, cfg, *, vix_override=None):
    """detect_regime with VIX from FRED + configured vol threshold."""
    vix = vix_override
    if vix is None:
        try:
            vix = get_macro_snapshot().vix
        except Exception:
            vix = None
    return detect_regime(
        market,
        vix=vix,
        vol_threshold_pct=cfg.regime.vol_threshold_pct,
    )


def _build_risk_state() -> RiskState:
    """Stub state — Plan 2 wires this to live P&L calculation."""
    return RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=False,
    )


@click.group()
def main() -> None:
    """Trading bot CLI."""
    # Auto-apply pending migrations on every CLI invocation. Idempotent —
    # alembic exits instantly when the schema is already at head. Set
    # TRADING_BOT_SKIP_MIGRATIONS=1 to skip (e.g. during tests that build
    # their own schema). Must run before any command opens state.db.
    from trading_bot.migrations_shim import ensure_migrations_at_head
    ensure_migrations_at_head()


@main.command()
def status() -> None:
    """Email a snapshot of the current paper account state."""
    import datetime as _dt_status
    import json as _json_status
    from trading_bot.email_alerts import StatusContext, build_status_email

    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    client = AlpacaClient(settings)
    account = client.get_account()
    positions = client.get_positions()

    # Open-order count + heartbeat — best-effort, never block the email.
    try:
        open_orders = client.get_open_order_symbols()
        open_order_count = len(open_orders)
    except Exception:
        open_order_count = 0

    last_heartbeat_age = None
    last_action = None
    try:
        hb_path = Path("data/heartbeat.json")
        if hb_path.exists():
            hb = _json_status.loads(hb_path.read_text())
            ts = _dt_status.datetime.fromisoformat(hb["ts"].replace("Z", "+00:00"))
            last_heartbeat_age = (
                _dt_status.datetime.now(_dt_status.timezone.utc) - ts
            ).total_seconds() / 60.0
            last_action = hb.get("last_action")
    except Exception:
        pass

    # Detect regime live so the subject reflects current market state.
    market = MarketDataClient(settings)
    try:
        regime = _live_regime(market, cfg).regime.value
    except Exception:
        regime = "unknown"

    pos_dicts = [
        {
            "symbol": p.symbol, "qty": p.qty,
            "avg_entry_price": p.avg_entry_price,
            "market_value": p.market_value,
            "unrealized_pl": p.unrealized_pl,
        }
        for p in positions
    ]

    # Guard: account.equity == $100,000 + 0 positions + regime resolution failed
    # is the unambiguous "freshly-initialized paper account / misconfigured Alpaca
    # client" signature. Sending this is misleading and pollutes the inbox; on
    # 2026-04-30 it accounted for 21 of 71 emails for the day.
    if (
        not pos_dicts
        and regime == "unknown"
        and Decimal(str(account.equity)) == Decimal("100000")
    ):
        click.echo(
            "[status] refusing to send — empty paper-account signature "
            "($100k / 0 positions / unknown regime). "
            "Check ALPACA_API_KEY/ALPACA_API_SECRET if this is unexpected.",
            err=True,
        )
        return

    ctx = StatusContext(
        as_of=_dt_status.datetime.now(_dt_status.timezone.utc),
        equity=account.equity, cash=account.cash,
        buying_power=account.buying_power, regime=regime,
        open_positions=pos_dicts, open_order_count=open_order_count,
        last_heartbeat_age_minutes=last_heartbeat_age,
        last_action=last_action,
    )
    email = build_status_email(ctx)

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject=email.subject, html_body=email.html_body,
                kind="status", recipient=cfg.email.to)
    click.echo(f"Sent status email to {cfg.email.to}")


@main.command("dry-run")
@click.option("--symbol", required=True)
@click.option("--side", type=click.Choice(["buy", "sell"]), required=True)
@click.option("--qty", required=True, type=str)
@click.option("--price", required=True, type=str)
@click.option("--stop", required=True, type=str)
@click.option(
    "--asset-class",
    type=click.Choice(["stock", "crypto", "option"]),
    default="stock",
)
@click.option(
    "--regime",
    type=click.Choice(["trending_up", "trending_down", "sideways", "risk_off"]),
    default="trending_up",
)
def dry_run(
    symbol: str, side: str, qty: str, price: str, stop: str, asset_class: str, regime: str
) -> None:
    """Validate a hypothetical order through the risk manager. No order is sent."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    client = AlpacaClient(settings)
    account = client.get_account()
    positions = client.get_positions()
    state = _build_risk_state()

    req = OrderRequest(
        symbol=symbol,
        qty=Decimal(qty),
        side=OrderSide(side),
        asset_class=AssetClass(asset_class),
        limit_price=Decimal(price),
        stop_loss_price=Decimal(stop),
    )
    rm = RiskManager(cfg)
    try:
        rm.check(req, account=account, positions=positions, state=state, regime=regime)
    except RiskRuleViolation as e:
        click.echo(f"REJECTED: {e}")
        raise SystemExit(1)
    click.echo(f"PASS: {symbol} {side} {qty} @ ${price} (stop ${stop}) — would be submitted.")


@main.command()
@click.option(
    "--regime",
    type=click.Choice(["trending_up", "trending_down", "sideways", "risk_off"]),
    default="trending_up",
)
def scan(regime: str) -> None:
    """Scan watchlist and place trades on signals (real paper orders)."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    watchlist = _load_active_universe()

    orch = _build_orchestrator(
        cfg=cfg, market=market, alpaca=alpaca, journal=journal,
        regime=regime, settings=settings,
    )
    result = orch.scan(watchlist=watchlist)
    write_last_scan(command="scan", regime=regime, universe_size=len(watchlist), result=result)
    click.echo(f"Scan complete — {len(result.decisions)} decisions:")
    for d in result.decisions:
        click.echo(f"  {d.symbol}: {d.action} ({d.reason})")


@main.command("daily-report")
@click.pass_context
def daily_report(ctx) -> None:
    """Deprecated alias for `daily-digest` — both use the data-driven 12-section
    template. Forwards to daily-digest so any cron / muscle memory keeps working."""
    click.echo("[daily-report] deprecated; forwarding to daily-digest", err=True)
    ctx.invoke(daily_digest)


@main.command("daily-digest")
def daily_digest() -> None:
    """Email the rebuilt 12-section daily digest (B3) — fully data-driven."""
    import datetime as _dt
    import json as _json
    from trading_bot.digest_data import gather_all
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    today = _dt.date.today()

    # ── ALL the live numbers ──────────────────────────────────────────
    data = gather_all(settings=settings, app_cfg=cfg, today=today)

    # ── Auxiliary metadata that doesn't fit DigestData ────────────────
    pending_promotions: list[dict] = []
    try:
        from trading_bot.lab_promotions import LabPromotionStore as _LPS
        _now = _dt.datetime.now(_dt.timezone.utc)
        pending_promotions = [
            p if isinstance(p, dict) else p.__dict__
            for p in _LPS().pending_validation(now=_now)
        ]
    except Exception:
        pass

    schedule_audit_warnings: list[dict] = []
    try:
        from trading_bot.schedule_audit import ScheduleAuditStore as _SAS
        _rows = _SAS().latest(audit_date=today)
        for _r in _rows:
            if getattr(_r, "ratio", 1.0) < 0.5:
                schedule_audit_warnings.append(
                    _r if isinstance(_r, dict) else _r.__dict__
                )
    except Exception:
        pass

    emails_sent_by_kind: dict[str, int] = {}
    try:
        from trading_bot.email_log import EmailLogStore as _ELS
        _today_start = _dt.datetime.combine(today, _dt.time.min).replace(
            tzinfo=_dt.timezone.utc
        )
        emails_sent_by_kind = _ELS().count_by_kind_since(_today_start)
    except Exception:
        pass

    git_sha = "unknown"
    version = "unknown"
    try:
        _active_path = Path("strategy/paper_active.json")
        if _active_path.exists():
            _meta = _json.loads(_active_path.read_text())
            git_sha = _meta.get("git_sha", "unknown")
            version = _meta.get("version", "unknown")
    except Exception:
        pass

    market = MarketDataClient(settings)
    try:
        regime = _live_regime(market, cfg).regime.value
    except Exception:
        regime = "unknown"

    # Realized P&L — derive from sum of today-closed trades' realized_pnl.
    # closed_trades_7d already filtered; pull today's subset.
    today_iso = today.isoformat()
    todays_realized = Decimal("0")
    for ct in data.closed_trades_7d:
        if str(ct.get("exit_time", ""))[:10] == today_iso:
            try:
                todays_realized += Decimal(str(ct.get("pnl", 0)))
            except Exception:
                pass

    ctx = DigestContext(
        date=today,
        starting_equity=data.starting_equity,
        ending_equity=data.ending_equity,
        realized_pnl=todays_realized,
        unrealized_pnl=data.unrealized_pnl,
        regime=regime,
        active_config_version=version,
        trades=data.trades_today,
        errors=data.errors,
        equity_30d=data.equity_30d,
        daily_loss_cap_pct=cfg.risk.daily_loss_limit_pct,
        weekly_loss_cap_pct=cfg.risk.weekly_loss_limit_pct,
        drawdown_pct=data.drawdown_pct,
        consecutive_losing_days=data.consecutive_losing_days,
        daily_loss_pct=data.daily_pnl_pct,
        weekly_loss_pct=data.weekly_pnl_pct,
        vix=data.vix,
        vol_threshold_pct=cfg.regime.vol_threshold_pct,
        positions=data.positions,
        closed_trades_7d=data.closed_trades_7d,
        pending_promotions=pending_promotions,
        schedule_audit_warnings=schedule_audit_warnings,
        daemon_blips=data.daemon_blips,
        emails_sent_by_kind=emails_sent_by_kind,
        git_sha=git_sha,
        version=version,
        wheel_open_cycles=data.wheel_open_cycles,
        wheel_pnl_mtd=data.wheel_pnl_mtd,
        wheel_collateral_pct=data.wheel_collateral_pct,
        wheel_win_rate=data.wheel_win_rate,
    )

    email = build_daily_digest_email(ctx)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject=email.subject, html_body=email.html_body,
                kind="digest", recipient=cfg.email.to)
    click.echo(f"[daily-digest] sent to {cfg.email.to}")


@main.command("reconcile")
def reconcile_cli() -> None:
    """Diff trade_journal vs Alpaca positions; write closed_trades rows
    for any entries whose position has disappeared. Idempotent."""
    from trading_bot.shared.alpaca_client import AlpacaClient
    from trading_bot.reconciler import reconcile
    from trading_bot.trade_journal import TradeJournal

    settings = Settings()
    cfg = load_config(CONFIG_PATH)

    client = AlpacaClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    closed_path = Path("data/closed_trades.db")

    report = reconcile(client=client, journal=journal, closed_trades_path=closed_path)

    click.echo(
        f"[reconcile] reconciled={report.reconciled_count} "
        f"unmatched={report.unmatched_count} errors={report.errors_count}"
    )
    for d in report.detail:
        click.echo(f"  {d['outcome']:12} {d.get('symbol', '?'):8} {d}")


@main.command()
@click.option("--apply", "apply_changes", is_flag=True, default=False,
              help="Apply proposed parameter changes to strategy/params.yaml.")
def evolve(apply_changes: bool) -> None:
    """Review closed-trade performance and propose rule tweaks."""
    closed = ClosedTradeStore(CLOSED_DB_PATH)
    trades = closed.all()
    stats = evaluate_performance(trades, min_trades=5)
    params = load_params(PARAMS_PATH)
    proposals = propose_rule_changes(stats, params)

    click.echo(f"Closed trades analyzed: {len(trades)}")
    if not stats:
        click.echo("Not enough trades per strategy yet (min 5). No analysis available.")
    else:
        for s in stats.values():
            click.echo(
                f"  {s.strategy}: {s.n_trades} trades, win {s.win_rate:.0%}, "
                f"PF {s.profit_factor:.2f}, P&L ${s.total_pnl:.2f}"
            )

    if proposals:
        click.echo(f"\n{len(proposals)} proposal(s):")
        for p in proposals:
            click.echo(f"  - {p.description}")
            click.echo(f"    {p.parameter}: {p.current_value} → {p.suggested_value} ({p.confidence})")
            click.echo(f"    Why: {p.rationale}")
        if apply_changes:
            new_params = apply_proposals(params, proposals)
            save_params(PARAMS_PATH, new_params)
            click.echo("\nApplied proposals to strategy/params.yaml.")
    else:
        click.echo("No rule changes proposed.")

    append_evolution_log(RULES_PATH, stats, proposals, applied=apply_changes)
    click.echo(f"Evolution log appended to {RULES_PATH}")


@main.command("full-run")
def full_run() -> None:
    """One-shot daily flow: regime → reconcile → scan → email report."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    closed = ClosedTradeStore(CLOSED_DB_PATH)
    watchlist = _load_active_universe()
    pnl_builder = PnlStateBuilder(settings, cfg)

    # 1. Reconcile any newly-closed trades from Alpaca
    rec = Reconciler(settings, journal, closed)
    rec_summary = rec.reconcile(lookback_days=30)
    click.echo(f"[reconcile] {rec_summary.new_closed} new closed trades")

    # 2. Detect live regime
    regime_reading = _live_regime(market, cfg)
    regime = regime_reading.regime.value
    click.echo(f"[regime] {regime} (vol {regime_reading.vol_annualized_pct:.1f}%, conf {regime_reading.confidence})")

    # 3. Scan + place trades through risk manager (with live P&L state)
    orch = _build_orchestrator(
        cfg=cfg, market=market, alpaca=alpaca, journal=journal,
        regime=regime, settings=settings,
        state_builder=pnl_builder.to_risk_state,
    )
    result = orch.scan(watchlist=watchlist)
    write_last_scan(command="full-run", regime=regime, universe_size=len(watchlist), result=result)
    click.echo(f"[scan] {len(result.decisions)} decisions:")
    for d in result.decisions:
        click.echo(f"  {d.symbol}: {d.action} ({d.reason})")

    # 4. Email daily digest
    import datetime as _dt_fr
    account = alpaca.get_account()
    _ctx_fr = DigestContext(
        date=_dt_fr.date.today(),
        starting_equity=account.equity,
        ending_equity=account.equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime=regime,
        active_config_version="unknown",
    )
    _email_fr = build_daily_digest_email(_ctx_fr)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject=f"Trading Bot — Daily Report ({regime})",
                html_body=_email_fr.html_body, kind="digest", recipient=cfg.email.to)
    click.echo(f"[email] sent to {cfg.email.to}")


@main.command("intel-scan")
def intel_scan() -> None:
    """Lightweight 15-min scan: regime + signals + place trades. Silent unless action taken."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    watchlist = _load_active_universe()
    pnl_builder = PnlStateBuilder(settings, cfg)

    regime_reading = _live_regime(market, cfg)
    regime = regime_reading.regime.value

    orch = _build_orchestrator(
        cfg=cfg, market=market, alpaca=alpaca, journal=journal,
        regime=regime, settings=settings,
        state_builder=pnl_builder.to_risk_state,
    )
    result = orch.scan(watchlist=watchlist)
    write_last_scan(command="intel-scan", regime=regime, universe_size=len(watchlist), result=result)
    placed = [d for d in result.decisions if d.action == "placed_order"]
    rejected = [d for d in result.decisions if d.action == "rejected_by_risk"]

    click.echo(f"[intel-scan] regime={regime} placed={len(placed)} rejected={len(rejected)}")
    for d in placed:
        click.echo(f"  PLACED {d.symbol}: {d.reason} (entry={d.entry_order_id})")
    for d in rejected:
        click.echo(f"  REJECTED {d.symbol}: {d.reason}")

    # Email only if action taken or risk-rejected (interesting events)
    if placed or rejected:
        _send_workflow_alert(
            settings=settings, cfg=cfg, workflow="intel-scan",
            regime=regime, decisions=result.decisions,
        )
        click.echo("[email] sent (action taken)")


def _send_workflow_alert(*, settings, cfg, workflow: str, regime: str, decisions) -> None:
    """Send a focused alert email summarising what one scan-flavoured
    workflow just did. Replaces the old pattern of rendering the full
    daily-digest skeleton with empty fields — see email_alerts.py for the
    dedicated template."""
    import datetime as _dt_alert
    from trading_bot.email_alerts import AlertContext, build_alert_email

    placed = [d for d in decisions if d.action == "placed_order"]
    rejected = [d for d in decisions if d.action == "rejected_by_risk"]
    skipped_intel = [d for d in decisions if d.action == "skipped_intel"]
    skipped_dq = [
        d for d in decisions
        if d.action in ("skipped_stale_data", "skipped_incomplete_data",
                        "skipped_restricted")
    ]

    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1

    git_sha = "unknown"
    version = "unknown"
    try:
        import json as _json_alert
        _active_path = Path("strategy/paper_active.json")
        if _active_path.exists():
            _meta = _json_alert.loads(_active_path.read_text())
            git_sha = _meta.get("git_sha", "unknown")
            version = _meta.get("version", "unknown")
    except Exception:
        pass

    ctx = AlertContext(
        as_of=_dt_alert.datetime.now(_dt_alert.timezone.utc),
        workflow=workflow, regime=regime,
        placed=[
            {"symbol": d.symbol, "reason": d.reason,
             "entry_order_id": d.entry_order_id} for d in placed
        ],
        rejected=[
            {"symbol": d.symbol, "reason": d.reason} for d in rejected
        ],
        skipped_intel=[
            {"symbol": d.symbol, "action": d.action, "reason": d.reason}
            for d in skipped_intel
        ],
        skipped_data_quality=[
            {"symbol": d.symbol, "action": d.action, "reason": d.reason}
            for d in skipped_dq
        ],
        decision_counts=counts,
        git_sha=git_sha, version=version,
    )
    email = build_alert_email(ctx)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password,
        to=cfg.email.to,
    )
    send_logged(
        sender=sender, subject=email.subject, html_body=email.html_body,
        kind="alert", recipient=cfg.email.to,
    )


@main.command("portfolio-watch")
def portfolio_watch() -> None:
    """Detect material portfolio changes since last snapshot. Email on alert."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)

    prev = load_snapshot(SNAPSHOT_PATH)
    curr = take_snapshot(alpaca)
    events = diff_snapshots(prev, curr, big_move_pct_threshold=2.0)
    save_snapshot(SNAPSHOT_PATH, curr)

    click.echo(f"[portfolio-watch] {len(events)} events (alerts: {sum(1 for e in events if e.severity == 'alert')})")
    for e in events:
        click.echo(f"  [{e.severity}] {e.kind}: {e.message}")

    if has_alerts(events):
        import datetime as _dt_pw
        from trading_bot.alerts import AlertEvent, queue_alert as _queue_alert_pw
        alert_count = sum(1 for e in events if e.severity == "alert")
        # Build a simple detail HTML — the full email shell is assembled by drain_alerts
        rows_html = "".join(
            f"<div style='margin:4px 0'><strong>{e.kind}</strong> "
            f"{'[' + e.symbol + '] ' if e.symbol else ''}{e.message}</div>"
            for e in events
        )
        detail_html = (
            f"<div style='font-size:13px;line-height:1.6'>"
            f"<p><strong>Equity:</strong> {curr.equity}</p>"
            f"{rows_html}</div>"
        )
        _now_pw = _dt_pw.datetime.now(_dt_pw.timezone.utc)
        _queue_alert_pw(AlertEvent(
            kind="portfolio_anomaly",
            severity="warn",
            title=f"Portfolio Alert — {alert_count} alert(s)",
            detail_html=detail_html,
            fired_at=_now_pw,
            dedup_key=f"portfolio_anomaly:{_dt_pw.date.today()}",
        ))
        click.echo("[alert] portfolio alert queued")


@main.command("rich-report")
@click.option("--period", type=click.Choice(["mid", "eod"]), default="mid",
              help="mid = 12:30 ET intraday review; eod = 16:30 ET end-of-day.")
def rich_report(period: str) -> None:
    """Comprehensive HTML email report: regime + macro + news + positions + decisions."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    watchlist = _load_active_universe()
    pnl_builder = PnlStateBuilder(settings, cfg)
    intel_agg = IntelligenceAggregator(settings)

    # 1. Reconcile any new closed trades
    closed = ClosedTradeStore(CLOSED_DB_PATH)
    Reconciler(settings, journal, closed).reconcile(lookback_days=30)

    # 2. Regime + scan (allows trades to be placed if signals appear)
    regime_reading = _live_regime(market, cfg)
    regime = regime_reading.regime.value
    orch = _build_orchestrator(
        cfg=cfg, market=market, alpaca=alpaca, journal=journal,
        regime=regime, settings=settings,
        state_builder=pnl_builder.to_risk_state,
    )
    result = orch.scan(watchlist=watchlist)
    write_last_scan(command="rich-report", regime=regime, universe_size=len(watchlist), result=result)

    # 3. Intelligence
    symbols = [w.symbol for w in watchlist]
    intel = intel_agg.gather(symbols)

    # 4. Portfolio diff vs last snapshot
    prev = load_snapshot(SNAPSHOT_PATH)
    curr = take_snapshot(alpaca)
    events = diff_snapshots(prev, curr, big_move_pct_threshold=2.0)
    save_snapshot(SNAPSHOT_PATH, curr)

    # 5. SPY daily change
    try:
        bars = market.get_daily_bars("SPY", lookback_days=2)
        spy_change = (Decimal(str((bars["close"].iloc[-1] / bars["close"].iloc[-2] - 1) * 100))
                      .quantize(Decimal("0.01")) if len(bars) >= 2 else Decimal("0"))
    except Exception:
        spy_change = Decimal("0")

    import datetime as _dt_rr
    account = alpaca.get_account()
    _ctx_rr = DigestContext(
        date=_dt_rr.date.today(),
        starting_equity=account.equity,
        ending_equity=account.equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime=regime,
        active_config_version="unknown",
        vix=intel.macro.vix,
    )
    _email_rr = build_daily_digest_email(_ctx_rr)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender,
                subject=f"Trading Bot — {period.upper()} Rich Report ({regime})",
                html_body=_email_rr.html_body, kind="digest", recipient=cfg.email.to)
    click.echo(f"[rich-report:{period}] sent ({len(result.decisions)} decisions, "
               f"VIX={intel.macro.vix}, {len(events)} events)")


@main.command("crypto-scan")
def crypto_scan() -> None:
    """24/7 crypto-only scan. Identical signal/risk logic to intel-scan but
    restricted to USD-quoted crypto pairs so the equity market being closed
    is irrelevant. Silent unless an order is placed or rejected.

    Phase 1G.3 — closes the bypass: prefers scout-elevated candidates from
    ``intel_candidates_crypto`` (those that passed Sasha/Lena/Diane's two-call
    debate). Falls back to the manual Alpaca crypto universe when no
    elevated candidates exist (cold start, post-rate-limit recovery, or any
    period when scout debate has not run yet) so the bot never goes silent.

    Set ``TRADING_BOT_CRYPTO_SCAN_BYPASS_SCOUT=1`` to force the legacy
    manual-universe path (useful for incident response or A/B comparison).
    """
    import os as _os

    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))

    bypass_scout = _os.environ.get("TRADING_BOT_CRYPTO_SCAN_BYPASS_SCOUT", "").strip() == "1"
    watchlist_source = "manual_universe"
    watchlist = []

    if not bypass_scout:
        try:
            from trading_bot.pipelines.crypto.scanner import load_elevated_watchlist
            from trading_bot.state_db import get_engine
            db_path = _os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
            state_engine = get_engine(db_path)
            elevated = load_elevated_watchlist(state_engine)
            if elevated:
                watchlist = elevated
                watchlist_source = "scout_elevated"
        except Exception as e:  # noqa: BLE001 — fall through, never break crypto trading
            click.echo(f"[crypto-scan] scout watchlist read failed ({e}); using manual universe")

    if not watchlist:
        watchlist = _load_active_universe(crypto_only=True)

    if not watchlist:
        click.echo("[crypto-scan] empty crypto universe — nothing to scan")
        return

    pnl_builder = PnlStateBuilder(settings, cfg)
    regime_reading = _live_regime(market, cfg)
    regime = regime_reading.regime.value

    orch = _build_orchestrator(
        cfg=cfg, market=market, alpaca=alpaca, journal=journal,
        regime=regime, settings=settings,
        state_builder=pnl_builder.to_risk_state,
    )
    result = orch.scan(watchlist=watchlist)
    write_last_scan(command="crypto-scan", regime=regime, universe_size=len(watchlist), result=result)
    placed = [d for d in result.decisions if d.action == "placed_order"]
    rejected = [d for d in result.decisions if d.action == "rejected_by_risk"]

    click.echo(f"[crypto-scan] source={watchlist_source} regime={regime} symbols={len(watchlist)} "
               f"placed={len(placed)} rejected={len(rejected)}")
    for d in placed:
        click.echo(f"  PLACED {d.symbol}: {d.reason} (entry={d.entry_order_id})")
    for d in rejected:
        click.echo(f"  REJECTED {d.symbol}: {d.reason}")

    if placed or rejected:
        _send_workflow_alert(
            settings=settings, cfg=cfg, workflow="crypto-scan",
            regime=regime, decisions=result.decisions,
        )
        click.echo("[email] sent (action taken)")


@main.command("eod-report")
def eod_report() -> None:
    """End-of-day rich HTML email; no scan, no order placement."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    intel_agg = IntelligenceAggregator(settings)

    regime = _live_regime(market, cfg).regime.value

    watchlist = _load_active_universe()
    intel = intel_agg.gather([w.symbol for w in watchlist])

    prev = load_snapshot(SNAPSHOT_PATH)
    curr = take_snapshot(alpaca)
    events = diff_snapshots(prev, curr, big_move_pct_threshold=2.0)
    save_snapshot(SNAPSHOT_PATH, curr)

    try:
        bars = market.get_daily_bars("SPY", lookback_days=2)
        spy_change = (Decimal(str((bars["close"].iloc[-1] / bars["close"].iloc[-2] - 1) * 100))
                      .quantize(Decimal("0.01")) if len(bars) >= 2 else Decimal("0"))
    except Exception:
        spy_change = Decimal("0")

    import datetime as _dt_eod
    account = alpaca.get_account()
    _ctx_eod = DigestContext(
        date=_dt_eod.date.today(),
        starting_equity=account.equity,
        ending_equity=account.equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime=regime,
        active_config_version="unknown",
        vix=intel.macro.vix,
    )
    _email_eod = build_daily_digest_email(_ctx_eod)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject=f"Trading Bot — EOD Report ({regime})",
                html_body=_email_eod.html_body, kind="digest", recipient=cfg.email.to)
    click.echo(f"[eod-report] sent to {cfg.email.to} (regime={regime}, "
               f"VIX={intel.macro.vix}, {len(events)} events)")


@main.command("screen-universe")
def screen_universe() -> None:
    """Snapshot the seed-list universe (Alpaca tradable ∩ CORE_LIQUID_TICKERS).

    Plan-6 made this a thin wrapper: the actual screening lives in
    `bot rank` (cache-fed grouped path) and `bot massive-refresh`
    (the writer). screen-universe is now mostly a debugging aid.
    """
    settings = Settings()
    alpaca = AlpacaClient(settings)
    assets = build_universe_from_seed_list(alpaca)
    write_universe_snapshot(
        assets,
        Path("strategy/latest_intelligence.md"),
        generated_at=datetime.now(timezone.utc),
    )
    click.echo(f"Wrote universe snapshot: {len(assets)} liquid assets (seed-list path)")


@main.command("backtest")
@click.option("--from", "from_date_str", default="2024-01-01", show_default=True,
              help="Backtest start date (YYYY-MM-DD).")
@click.option("--to", "to_date_str", default=None,
              help="Backtest end date (YYYY-MM-DD). Defaults to today.")
@click.option("--symbols", "symbols_csv",
              default="SPY,QQQ,AAPL,MSFT,NVDA,AMD,GOOGL,META,AMZN,TSLA,BTC/USD,ETH/USD",
              show_default=True,
              help="Comma-separated symbols.")
@click.option("--strategies", "strategies_csv", default="momentum,mean_reversion",
              show_default=True)
@click.option("--max-hold-days", default=60, show_default=True, type=int)
@click.option("--starting-equity", default=15000, show_default=True, type=int)
@click.option("--slippage-bps", default=0.0, show_default=True, type=float)
@click.option("--no-refresh", is_flag=True, default=False,
              help="Skip cache warm-up (use whatever is in data/backtest_bars.db).")
@click.option("--trailing-stop", is_flag=True, default=False,
              help="Enable trailing stops (ratchet to breakeven at +3%, "
                   "trail at 50%% of peak above 5%%). Empirically worse than "
                   "off on momentum/large-caps; default off.")
@click.option("--report-path", default="strategy/backtest_results.md", show_default=True)
def backtest(from_date_str: str, to_date_str: str | None,
             symbols_csv: str, strategies_csv: str,
             max_hold_days: int, starting_equity: int, slippage_bps: float,
             no_refresh: bool, trailing_stop: bool, report_path: str) -> None:
    """Replay historical bars through real strategy + risk_manager code paths."""
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    from trading_bot.backtest.bar_store import BarStore
    from trading_bot.backtest.metrics import compute_metrics
    from trading_bot.backtest.reporter import write_markdown_report
    from trading_bot.backtest.simulator import (
        BacktestStore,
        Backtester,
        fetch_vix_history,
    )

    from_date = _date.fromisoformat(from_date_str)
    to_date = _date.fromisoformat(to_date_str) if to_date_str else _date.today()
    symbols = [s.strip() for s in symbols_csv.split(",") if s.strip()]
    strategy_names = tuple(s.strip() for s in strategies_csv.split(",") if s.strip())

    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    market = MarketDataClient(settings)

    bar_store = BarStore("data/backtest_bars.db")

    if not no_refresh:
        click.echo(f"[backtest] warming bar cache for {len(symbols)} symbols ({from_date} → {to_date})...")
        warm = bar_store.warm(symbols, from_date=from_date, to_date=to_date, market=market)
        for sym, count in warm.items():
            note = f"{count} new" if count > 0 else ("cached" if count == 0 else "FETCH FAILED")
            click.echo(f"  {sym}: {note}")

    click.echo("[backtest] fetching VIX history (FRED)...")
    vix_series = fetch_vix_history(from_date, to_date)
    click.echo(f"  VIX: {len(vix_series)} dates")

    click.echo(f"[backtest] running simulator ({from_date} → {to_date})...")
    bt = Backtester(
        config=cfg, bar_store=bar_store,
        starting_equity=_Decimal(str(starting_equity)),
        max_hold_days=max_hold_days,
        slippage_bps=slippage_bps,
        vix_series=vix_series,
        enable_trailing_stop=trailing_stop,
    )
    result = bt.run(
        from_date=from_date, to_date=to_date,
        symbols=symbols, strategy_names=strategy_names,
    )
    metrics = compute_metrics(result)

    # Persist trades
    store = BacktestStore("data/backtest_trades.db")
    for t in result.trades:
        store.append(t)

    # Write markdown report
    write_markdown_report(result, metrics, report_path)

    click.echo(
        f"[backtest] run_id={result.run_id} trades={len(result.trades)} "
        f"halted_days={result.halted_days} skipped_risk={result.skipped_by_risk} "
        f"skipped_no_bars={result.skipped_no_bars}"
    )
    click.echo(
        f"[backtest] equity {starting_equity} → {result.ending_equity:,.2f} "
        f"({(result.ending_equity / _Decimal(str(starting_equity)) - 1) * 100:+.2f}%)"
    )
    click.echo(f"[backtest] report: {report_path}")
    if metrics.overall.sharpe_daily_ann is not None:
        click.echo(
            f"[backtest] overall: PF={metrics.overall.profit_factor or '—'}, "
            f"Sharpe={metrics.overall.sharpe_daily_ann}, "
            f"win={metrics.overall.win_rate_pct}%"
        )


@main.command("news-warm")
@click.option("--lookback-days", default=3, show_default=True, type=int)
def news_warm(lookback_days: int) -> None:
    """Refresh per-ticker news sentiment for the active trading universe.
    Stores aggregate scores in data/news_sentiment.db. Run on cron before
    each scan window so entries can gate on freshly-computed scores."""
    from trading_bot.news_sentiment import warm_for_symbols

    universe = _load_active_universe()
    symbols = [e.symbol for e in universe if e.asset_class != "crypto"]
    if not symbols:
        click.echo("[news-warm] no stock symbols in active universe — skipping")
        return
    click.echo(f"[news-warm] fetching sentiment for {len(symbols)} symbols "
               f"(lookback {lookback_days}d)...")

    readings = warm_for_symbols(symbols, lookback_days=lookback_days)
    have = sum(1 for r in readings.values() if r is not None)
    no_data = sum(1 for r in readings.values() if r is None)

    click.echo(f"[news-warm] cached={have} no-data={no_data}")
    # Surface the most-bearish + most-bullish for human eyeballs
    scored = sorted(
        [r for r in readings.values() if r is not None],
        key=lambda r: r.score,
    )
    if scored:
        click.echo("  bearish:")
        for r in scored[:5]:
            click.echo(f"    {r.symbol:6} {r.score:+.2f}  ({r.dominant_label}, "
                       f"{r.n_articles} articles)")
        click.echo("  bullish:")
        for r in reversed(scored[-5:]):
            click.echo(f"    {r.symbol:6} {r.score:+.2f}  ({r.dominant_label}, "
                       f"{r.n_articles} articles)")


@main.command("verify-stops")
def verify_stops() -> None:
    """Sweep open positions, auto-protect or flatten any unprotected ones,
    email a summary of actions taken. Stocks act 24/7 for stop placement;
    market-flatten for stocks defers outside US RTH (Alpaca rejects market
    sells off-hours). Crypto acts 24/7."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    from trading_bot.shared.alpaca_client import AlpacaClient
    from trading_bot.market_data import MarketDataClient
    from trading_bot.position_protection import evaluate_and_act
    from trading_bot.supervisor import _is_market_hours_et

    settings = Settings()
    cfg = load_config(CONFIG_PATH)

    try:
        alpaca = AlpacaClient(settings)
        positions = alpaca.get_positions()
        # nested=True returns parents WITH their child legs in `.legs`. Without
        # it, bracket-order stop legs (the protective stop attached to a BUY
        # parent) don't show up in the OPEN list, so we'd think the position
        # was unprotected and try to place a duplicate stop — Alpaca then
        # rejects it. This is what produced the 13 [BAD] verify-stops emails
        # for ARM on 2026-04-30 between 13:50 and 19:50 UTC.
        open_orders = alpaca._client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.OPEN, limit=200, nested=True
            )
        )
    except Exception as e:
        click.echo(f"[verify-stops] alpaca query failed: {e}")
        return  # do not raise SystemExit — would kill the APScheduler worker.

    def _canon(sym: str) -> str:
        return str(sym).replace("/", "").upper()

    def _is_live_stop(o) -> bool:
        type_ok = str(getattr(o, "type", "")).lower().endswith(
            ("stop", "stop_limit")
        )
        if not type_ok:
            return False
        # Treat any non-terminal state as protective. ACCEPTED legs of a bracket
        # whose parent has filled are live-but-pending-route from Alpaca's view
        # and will trigger; we must not classify them as missing.
        terminal = {"filled", "canceled", "expired", "rejected", "replaced",
                    "done_for_day", "suspended"}
        return str(getattr(o, "status", "")).split(".")[-1].lower() not in terminal

    stops_by_symbol: dict[str, list] = {}
    def _ingest(order) -> None:
        if _is_live_stop(order):
            stops_by_symbol.setdefault(_canon(order.symbol), []).append(order)
        for leg in (getattr(order, "legs", None) or []):
            _ingest(leg)
    for o in open_orders:
        _ingest(o)

    unprotected = [p for p in positions if _canon(p.symbol) not in stops_by_symbol]

    click.echo(
        f"[verify-stops] positions={len(positions)} "
        f"stops={sum(len(v) for v in stops_by_symbol.values())} "
        f"unprotected={len(unprotected)}"
    )

    if not unprotected:
        return

    market_data = MarketDataClient(settings)
    actions = evaluate_and_act(
        client=alpaca,
        market_data=market_data,
        unprotected=unprotected,
        stop_pct=Decimal(str(cfg.risk.unprotected_stop_pct)),
        now_in_market_hours=_is_market_hours_et(),
    )

    for a in actions:
        click.echo(f"  {a.outcome.upper():22} {a.symbol:10} qty={a.qty}")

    import datetime as dt_mod
    from trading_bot.alerts import AlertEvent, queue_alert as _queue_alert_vs
    _queue_alert_vs(AlertEvent(
        kind="auto_protect_summary",
        severity="bad" if any(a.outcome == "failed" for a in actions) else "info",
        title=open_positions_email_subject(actions),
        detail_html=build_open_positions_email_html(actions, total_positions=len(positions)),
        fired_at=dt_mod.datetime.now(dt_mod.timezone.utc),
        dedup_key=f"auto_protect:{actions[0].symbol}:{dt_mod.date.today()}",
    ))
    click.echo(f"[verify-stops] alert queued")


@main.command("vip-scan")
def vip_scan() -> None:
    """Poll VIP tweet feeds (Truth Social RSS). Alert-only — never trades."""
    from trading_bot.vip_tweets import scan as run_vip_scan

    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    result = run_vip_scan()

    high = [p for p in result.new_posts if p.severity == "high"]
    med = [p for p in result.new_posts if p.severity == "med"]
    click.echo(
        f"[vip-scan] handles={result.handles_polled} new={len(result.new_posts)} "
        f"high={len(high)} med={len(med)} errors={len(result.errors)}"
    )
    for p in result.new_posts:
        click.echo(f"  [{p.severity.upper()}] {p.handle}: {p.text[:140]} ({p.severity_reason})")
    for e in result.errors:
        click.echo(f"  ERROR: {e}")

    if not high:
        return  # alerts only fire on HIGH

    import datetime as _dt_vip
    from trading_bot.alerts import AlertEvent, queue_alert as _queue_alert_vip
    html = build_vip_alert_email_html(high)
    _now_vip = _dt_vip.datetime.now(_dt_vip.timezone.utc)
    for _post in high:
        _queue_alert_vip(AlertEvent(
            kind="vip_tweet",
            severity="warn",
            title=f"VIP tweet — {_post.handle}: {_post.text[:80]}",
            detail_html=html,
            fired_at=_now_vip,
            # Bucket F: VipPost.post_id, not .id — pre-Bucket-F getattr
            # always missed and fell back to text[:32], collapsing retweets
            # and quotes whose first 32 chars matched into a single alert.
            dedup_key=f"vip:{getattr(_post, 'post_id', _post.text[:32])}",
        ))
    click.echo(f"[vip-scan] {len(high)} high-severity tweet alert(s) queued")


@main.command("dashboard")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code change (dev).")
def dashboard(host: str, port: int, reload: bool) -> None:
    """Start the local trading-bot dashboard at http://HOST:PORT (default 127.0.0.1:8765)."""
    from trading_bot.dashboard.app import run
    click.echo(f"Dashboard starting at http://{host}:{port} — Ctrl+C to stop")
    run(host=host, port=port, reload=reload)


@main.command("daemon")
def daemon_cmd() -> None:
    """Run the trading bot daemon (long-running APScheduler-driven process)."""
    from trading_bot.shared.daemon import main as daemon_main
    raise SystemExit(daemon_main())


@main.command("supervisor")
def supervisor_cmd() -> None:
    """Run the trading bot supervisor (watchdog + drawdown sentinel)."""
    from trading_bot.supervisor import main as supervisor_main
    raise SystemExit(supervisor_main())


@main.command("lab")
def lab_cmd() -> None:
    """Run the trading bot lab (nightly param search + auto-promote)."""
    from trading_bot.lab import main as lab_main
    raise SystemExit(lab_main())


@main.command("lab-backfill")
@click.option("--symbols", default="SPY", show_default=True,
              help="Comma-separated tickers to backfill into the lab's bar cache.")
@click.option("--months", default=30, show_default=True, type=int,
              help="How many months of history to fetch (default sized for 6-fold "
                   "walk-forward: 12mo train + 5×3mo tests).")
@click.option("--db-path", default="data/massive_grouped.db", show_default=True,
              help="Bar cache DB the lab reads from (BarStore schema).")
def lab_backfill(symbols: str, months: int, db_path: str) -> None:
    """One-shot backfill of historical daily bars into the lab's bar cache.

    The lab's walk-forward harness needs ~30 months of history to run a full
    6-fold sweep. This command fetches that range from Alpaca and upserts
    into the BarStore at db_path.
    """
    from datetime import date, timedelta

    from trading_bot.backtest.bar_store import BarStore

    settings = Settings()
    market = MarketDataClient(settings)

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    to_date = date.today()
    from_date = to_date - timedelta(days=months * 31)

    bar_store = BarStore(db_path)
    click.echo(
        f"[lab-backfill] warming {len(syms)} symbol(s) "
        f"{from_date} → {to_date} (~{months}mo) into {db_path}..."
    )
    results = bar_store.warm(syms, from_date=from_date, to_date=to_date, market=market)
    for sym, count in results.items():
        note = f"{count} new" if count > 0 else ("cached" if count == 0 else "FETCH FAILED")
        click.echo(f"  {sym}: {note}")


# Phase 6: bot promote — manual gate for paper → live trading.
from trading_bot.promote_cli import register_promote_command  # noqa: E402

register_promote_command(main)


@main.command("massive-refresh")
@click.option("--days", default=5, show_default=True, type=int,
              help="How many trading days back to ensure are cached.")
@click.option("--news/--no-news", default=False, show_default=True,
              help="Also refresh news sentiment cache for the active stock universe.")
def massive_refresh(days: int, news: bool) -> None:
    """Refresh the Massive grouped cache for the last N trading days.

    Idempotent: skips dates already cached. Walks back day-by-day from
    today, attempting up to `days + 7` calendar days back to find the
    most recent N actual trading days. Exits non-zero only if the cache
    ends up with zero entries within the last 7 days (i.e. a hard
    failure, not just a holiday).
    """
    from datetime import timedelta as _td

    from trading_bot.massive_cache import MassiveGroupedCache
    from trading_bot.massive_client import (
        MassiveAuthError,
        MassiveClient,
        MassiveRateLimitError,
    )

    cache = MassiveGroupedCache()
    try:
        massive = MassiveClient()
    except MassiveAuthError as e:
        click.echo(f"[massive-refresh] auth error: {e}", err=True)
        raise SystemExit(1)

    today = datetime.now(timezone.utc).date()
    found_trading_days = 0
    calls_made = 0
    cached_dates: list = []
    skipped_dates: list = []
    failed_dates: list = []

    cur = today
    tries = 0
    while found_trading_days < days and tries < days + 7:
        cur -= _td(days=1)
        tries += 1

        if cache.has(cur):
            skipped_dates.append(cur)
            found_trading_days += 1
            continue

        try:
            df = massive.daily_grouped(cur)
            calls_made += 1
        except MassiveRateLimitError as e:
            click.echo(f"[massive-refresh] rate-limited on {cur}: {e}", err=True)
            failed_dates.append(cur)
            continue
        except MassiveAuthError as e:
            # Polygon 403s "Attempted to request today's data before end of
            # day" if we ask for the current ET trading session before close.
            # Skip and try older — not a fatal error.
            msg = str(e).lower()
            if "today" in msg or "before end of day" in msg:
                click.echo(f"[massive-refresh] {cur}: not yet available (pre-close), skipping")
                continue
            click.echo(f"[massive-refresh] auth error on {cur}: {e}", err=True)
            raise SystemExit(1)

        if df.empty:
            continue

        n = cache.store(cur, df)
        cached_dates.append((cur, n))
        found_trading_days += 1

    cache.evict_older_than(days=30)

    click.echo(
        f"[massive-refresh] calls={calls_made} "
        f"cached_new={len(cached_dates)} skipped_existing={len(skipped_dates)} "
        f"failed={len(failed_dates)}"
    )
    for d, n in cached_dates:
        click.echo(f"  + {d}: {n} tickers")
    for d in skipped_dates:
        click.echo(f"  = {d}: already cached")
    for d in failed_dates:
        click.echo(f"  ! {d}: failed")

    if news:
        from trading_bot.news_sentiment import warm_for_symbols

        universe = _load_active_universe()
        symbols = [e.symbol for e in universe if e.asset_class != "crypto"]
        if not symbols:
            click.echo("[massive-refresh:news] empty stock universe — skipping")
        else:
            click.echo(f"[massive-refresh:news] warming {len(symbols)} symbols...")
            readings = warm_for_symbols(symbols)
            have = sum(1 for r in readings.values() if r is not None)
            click.echo(f"[massive-refresh:news] cached={have} no-data={len(readings) - have}")

    fresh = cache.latest(max_age_days=7)
    if fresh is None:
        click.echo("[massive-refresh] WARNING: cache has no entries within 7 days", err=True)
        raise SystemExit(2)
    on_date, df = fresh
    click.echo(f"[massive-refresh] freshest cached day: {on_date} ({len(df)} tickers)")


@main.command("rank")
def rank_command() -> None:
    """Run stage-1 + stage-2 screener; write strategy/opportunities.md.

    Reads the Massive grouped cache (filled by `bot massive-refresh`)
    for the universe; falls through to CORE_LIQUID_TICKERS seed list
    if cache is empty. Never calls Massive directly — that path is
    the refresh task's responsibility.
    """
    settings = Settings()
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)

    def bar_loader_short(symbol: str):
        try:
            return market.get_daily_bars(symbol, lookback_days=20)
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    def bar_loader_long(symbol: str):
        try:
            return market.get_daily_bars(symbol, lookback_days=60)
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    from trading_bot.massive_cache import MassiveGroupedCache
    cache = MassiveGroupedCache()
    cached = cache.latest(max_age_days=5)

    if cached is not None:
        on_date, grouped_df = cached
        click.echo(f"[rank] cache hit (date={on_date}, {len(grouped_df)} tickers)")

        def _grouped():
            return grouped_df

        universe = build_universe_from_grouped(
            alpaca,
            massive_grouped_loader=_grouped,
            crypto_bar_loader=bar_loader_short,
        )

        if not universe:
            click.echo("[rank] grouped path empty after liquidity filter — "
                       "falling back to seed list")
            universe = build_universe_from_seed_list(alpaca)
        else:
            stocks = [a for a in universe if "crypto" not in a.asset_class.lower()]
            cryptos = [a for a in universe if "crypto" in a.asset_class.lower()]
            stocks.sort(key=lambda a: a.avg_dollar_volume, reverse=True)
            universe = stocks[:200] + cryptos
            click.echo(
                f"[rank] pre-shortlist (top 200 stocks by ADV + {len(cryptos)} crypto)"
            )
    else:
        click.echo("[rank] cache miss — using CORE_LIQUID_TICKERS seed list")
        universe = build_universe_from_seed_list(alpaca)

    click.echo(f"[rank] universe size: {len(universe)} assets")

    shortlist = build_stage1_shortlist(
        universe, bar_loader=bar_loader_short, top_n=100,
    )

    lanes = [MomentumLane(), MeanReversionLane(), BreakoutLane()]
    result = run_stage2(shortlist, lanes=lanes, bar_loader=bar_loader_long)
    write_opportunities_snapshot(
        result,
        Path("strategy/opportunities.md"),
        generated_at=datetime.now(timezone.utc),
        shortlist=shortlist,
    )
    click.echo(f"Stage-2 ranked {len(result.candidates)} candidates across {len(lanes)} lanes")


@main.command("nightly-review")
def nightly_review_cli() -> None:
    """Bucket G: build + send the nightly self-review email.

    Read-only summary: decision rollup, drift watch, freshness audit,
    risk state, system health. Wired into the daemon at 17:00 ET; this
    CLI lets the operator invoke it manually for testing or after-hours
    triage.
    """
    from trading_bot.nightly_review import run_nightly_review
    from trading_bot.state_db import get_engine
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password,
        to=cfg.email.to,
    )
    engine = get_engine(STATE_DB_PATH)
    review = run_nightly_review(
        engine=engine, sender=sender, recipient=cfg.email.to,
    )
    click.echo(
        f"Sent nightly review to {cfg.email.to}: "
        f"placed={review.decisions.placed_order} "
        f"drift_findings={len(review.drift)}"
    )


@main.command("midday-snapshot")
def midday_snapshot_cli() -> None:
    """Build + send the midday snapshot email at 12:00 ET — data-driven."""
    import datetime as _dt_ms
    import json as _json_ms
    from trading_bot.digest_data import gather_all
    settings = Settings()
    cfg = load_config(CONFIG_PATH)

    data = gather_all(settings=settings, app_cfg=cfg)

    try:
        market = MarketDataClient(settings)
        regime = _live_regime(market, cfg).regime.value
    except Exception:
        regime = "unknown"

    git_sha = "unknown"
    version = "unknown"
    try:
        _active_path = Path("strategy/paper_active.json")
        if _active_path.exists():
            _meta = _json_ms.loads(_active_path.read_text())
            git_sha = _meta.get("git_sha", "unknown")
            version = _meta.get("version", "unknown")
    except Exception:
        pass

    # Today's realized = sum of today's closed-trade P&L
    today_iso = _dt_ms.date.today().isoformat()
    todays_realized = Decimal("0")
    todays_trades_dicts: list[dict] = []
    for ct in data.closed_trades_7d:
        if str(ct.get("exit_time", ""))[:10] == today_iso:
            try:
                todays_realized += Decimal(str(ct.get("pnl", 0)))
            except Exception:
                pass
    # Trades_today for midday = today's entry rows (TradeRow → dict).
    # qty/price stay numeric — email_midday.py:66 formats price with `:,.2f`
    # which raises ValueError on str inputs.
    for tr in data.trades_today:
        todays_trades_dicts.append({
            "time": tr.time.strftime("%H:%M"),
            "side": tr.side,
            "symbol": tr.symbol,
            "qty": tr.qty,
            "price": float(tr.price),
            "strategy": tr.strategy,
        })

    from trading_bot.email_midday import SnapshotContext, build_midday_snapshot_email
    ctx = SnapshotContext(
        as_of=_dt_ms.datetime.now(_dt_ms.timezone.utc),
        equity=data.ending_equity,
        starting_equity=data.starting_equity,
        realized_pnl_today=todays_realized,
        unrealized_pnl=data.unrealized_pnl,
        regime=regime,
        positions=data.positions,
        trades_today=todays_trades_dicts,
        watchlist_signals=[],     # screener near-miss list — not yet exposed
        daily_loss_pct=data.daily_pnl_pct,
        drawdown_pct=data.drawdown_pct,
        daily_loss_cap_pct=cfg.risk.daily_loss_limit_pct,
        drawdown_cap_pct=20.0,
        version=version,
        git_sha=git_sha,
    )
    email = build_midday_snapshot_email(ctx)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject=email.subject, html_body=email.html_body,
                kind="midday", recipient=cfg.email.to)
    click.echo(f"[midday-snapshot] sent to {cfg.email.to}")


@main.command("alert-drain")
def alert_drain_cli() -> None:
    """Drain queued alerts if 20-min cooldown elapsed."""
    from trading_bot.alerts import drain_alerts
    n = drain_alerts()
    click.echo(f"[alert-drain] drained {n} event(s)")


# ─── Phase 5: Wheel CLI subcommands ─────────────────────────────────────────
# `wheel-scan` and `wheel-manage` raise SystemExit("…Phase 6") since the
# WheelDeps wiring lives there. `wheel-status` queries the state DB directly
# (no Alpaca needed). `wheel-close` also defers to Phase 6.


@main.command("wheel-status")
def wheel_status_cli() -> None:
    """Print active wheel cycles from the state DB."""
    from trading_bot.options.wheel_state import WheelStateRepo
    from trading_bot.state_db import get_engine

    db_path = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
    engine = get_engine(db_path)
    try:
        rows = WheelStateRepo(engine).list_active()
    except Exception as e:
        click.echo(f"[wheel-status] db query failed: {e}")
        click.echo("No open wheel cycles.")
        return

    if not rows:
        click.echo("No open wheel cycles.")
        return

    click.echo(f"[wheel-status] {len(rows)} active cycle(s):")
    for c in rows:
        contract = c.cc_contract or c.csp_contract or "—"
        click.echo(
            f"  {c.symbol:6s}  phase={c.phase:10s}  contract={contract}"
        )


def _build_wheel_runtime():
    """Construct the wheel runtime stack used by the CLI subcommands.
    Returns (deps, app_cfg) tuple — deps is None if wheel.enabled is False."""
    from trading_bot.alerts import queue_alert as _qa
    from trading_bot.shared.daemon import (
        _MacroSnapshotter, _RegimeDetectorAdapter, _build_wheel_deps,
    )
    from trading_bot.shared.risk_manager import RiskManager
    from trading_bot.state_db import get_engine

    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    if not cfg.wheel.enabled:
        return None, cfg
    db_path = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
    engine = get_engine(db_path)
    alpaca = AlpacaClient(settings)
    risk = RiskManager(cfg)
    macro = _MacroSnapshotter()
    regime = _RegimeDetectorAdapter(settings, cfg)
    deps = _build_wheel_deps(
        settings=settings, app_cfg=cfg, state_engine=engine,
        alpaca_client=alpaca, risk_manager=risk,
        intelligence_macro=macro, regime_detector=regime, queue_alert=_qa,
    )
    return deps, cfg


@main.command("wheel-scan")
def wheel_scan_cli() -> None:
    """Run a one-shot wheel scan (open CSP / open CC entries)."""
    from trading_bot.options.wheel_runner import run_wheel_scan
    deps, cfg = _build_wheel_runtime()
    if deps is None:
        click.echo("wheel disabled in config")
        return
    run_wheel_scan(deps)
    click.echo("[wheel-scan] complete")


@main.command("wheel-manage")
def wheel_manage_cli() -> None:
    """Run a one-shot wheel manage pass (take-profit / DTE close / rolls)."""
    from trading_bot.options.wheel_runner import run_wheel_manage
    deps, cfg = _build_wheel_runtime()
    if deps is None:
        click.echo("wheel disabled in config")
        return
    run_wheel_manage(deps)
    click.echo("[wheel-manage] complete")


@main.command("wheel-close")
@click.argument("symbol")
def wheel_close_cli(symbol: str) -> None:
    """Emergency-close the active wheel short for SYMBOL at the current mid."""
    from trading_bot.options.wheel_runner import _close_short
    from trading_bot.options.wheel_state import WheelStateRepo

    deps, cfg = _build_wheel_runtime()
    if deps is None:
        click.echo("wheel disabled in config")
        return
    repo = WheelStateRepo(deps.engine)
    cyc = repo.get_active(symbol=symbol.upper())
    if cyc is None:
        click.echo(f"[wheel-close] no active cycle for {symbol}")
        raise SystemExit(1)
    contract = cyc.cc_contract or cyc.csp_contract
    if not contract:
        click.echo(f"[wheel-close] cycle has no open contract for {symbol}")
        raise SystemExit(1)
    try:
        snap = deps.option_alpaca.snapshot_for_contract(contract)
        mid = (snap.bid + snap.ask) / 2.0
    except Exception as e:
        click.echo(f"[wheel-close] snapshot failed: {e}")
        raise SystemExit(1)
    _close_short(deps, cyc, contract, kind="wheel_dte_close",
                 price=Decimal(str(round(mid, 2))))
    click.echo(f"[wheel-close] closed {contract} for {symbol} @ {mid:.2f}")


@main.command("iv-capture")
def iv_capture_cli() -> None:
    """Capture today's ATM 30-day IV for each allowlisted symbol. Writes to
    option_iv_history. The wheel-scan @ 10:15 ET reads from this table."""
    from trading_bot.shared.daemon import _build_iv_capture_runner
    from trading_bot.state_db import get_engine
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    if not cfg.wheel.enabled:
        click.echo("wheel disabled in config")
        return
    db_path = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
    engine = get_engine(db_path)
    runner = _build_iv_capture_runner(settings=settings, app_cfg=cfg, state_engine=engine)
    runner()
    click.echo("[iv-capture] complete")


@main.command("wheel-universe-build")
def wheel_universe_build_cli() -> None:
    """Discover wheel-eligible symbols from Alpaca optionable + Finnhub
    quality filters; write to wheel_universe_cache. First-ever run takes
    ~100 min (Finnhub free 60/min × ~6,000 names). Subsequent runs only
    re-check 14d-stale entries."""
    from trading_bot.shared.daemon import _build_universe_builder_runner
    from trading_bot.state_db import get_engine
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    if not cfg.wheel.enabled:
        click.echo("wheel disabled in config")
        return
    db_path = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
    engine = get_engine(db_path)
    runner = _build_universe_builder_runner(settings=settings, app_cfg=cfg, state_engine=engine)
    runner()
    click.echo("[wheel-universe-build] complete")


@main.command("schedule-audit")
def schedule_audit_cli() -> None:
    """Audit today's cron job firings vs expected. Writes to schedule_audits."""
    import datetime as dt_mod
    from pathlib import Path
    from trading_bot.schedule_audit import run_audit

    today = dt_mod.date.today()
    report = run_audit(audit_date=today, runs_dir=Path("runs"))
    flagged = [r for r in report if r["ratio"] < 0.5]
    click.echo(f"[schedule-audit] {len(report)} jobs audited, {len(flagged)} flagged")
    for r in flagged:
        click.echo(f"  ! {r['job_id']:24} {r['actual']}/{r['expected']} (ratio {r['ratio']:.2f})")


if __name__ == "__main__":
    main()
