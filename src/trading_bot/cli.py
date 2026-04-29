from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import os

import click

from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderRequest, OrderSide
from trading_bot.config import Settings, load_config
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
    build_alert_email_html,
    build_open_positions_email_html,
    build_vip_alert_email_html,
    open_positions_email_subject,
)
from trading_bot.email_digest import build_daily_digest_email, DigestContext, TradeRow as DigestTradeRow
from trading_bot.risk_manager import RiskManager, RiskState
from trading_bot.state import load_watchlist
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

ACTIVE_UNIVERSE_TOP_N_STOCKS = 25


def _is_usd_crypto(symbol: str) -> bool:
    """Filter out wrapped/cross-quoted crypto pairs (BTC/USDC, LINK/BTC, etc.)
    so we don't double-count the same underlying asset."""
    return symbol.endswith("/USD")


def _load_active_universe(*, crypto_only: bool = False):
    """Active trading universe = top-N ranked stocks from opportunities.md
    + USD-quoted crypto pairs (from opportunities.md and watchlist.yaml).
    Falls back to full watchlist.yaml if opportunities.md is missing/empty.

    If crypto_only=True, returns only the crypto subset — used by the 24/7
    crypto-scan loop when the equity market is closed.
    """
    fallback = load_watchlist(WATCHLIST_PATH)
    ranked = load_ranked_watchlist(OPPORTUNITIES_PATH)

    if not ranked:
        if crypto_only:
            return [e for e in fallback if e.asset_class == "crypto" and _is_usd_crypto(e.symbol)]
        return fallback

    ranked_crypto = [
        e for e in ranked if e.asset_class == "crypto" and _is_usd_crypto(e.symbol)
    ]
    fallback_crypto = [
        e for e in fallback if e.asset_class == "crypto" and _is_usd_crypto(e.symbol)
    ]

    if crypto_only:
        seen: set[str] = set()
        out: list = []
        for e in ranked_crypto + fallback_crypto:
            if e.symbol in seen:
                continue
            seen.add(e.symbol)
            out.append(e)
        return out

    stocks = [e for e in ranked if e.asset_class != "crypto"][:ACTIVE_UNIVERSE_TOP_N_STOCKS]
    seen = set()
    out = []
    for e in stocks + ranked_crypto + fallback_crypto:
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


@main.command()
def status() -> None:
    """Email a snapshot of the current paper account state."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    client = AlpacaClient(settings)
    account = client.get_account()
    positions = client.get_positions()

    import datetime as _dt_status
    _ctx_status = DigestContext(
        date=_dt_status.date.today(),
        starting_equity=account.equity,
        ending_equity=account.equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="trending_up",
        active_config_version="unknown",
    )
    _email_status = build_daily_digest_email(_ctx_status)

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject="Trading Bot — Status", html_body=_email_status.html_body,
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

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
    )
    result = orch.scan(watchlist=watchlist)
    write_last_scan(command="scan", regime=regime, universe_size=len(watchlist), result=result)
    click.echo(f"Scan complete — {len(result.decisions)} decisions:")
    for d in result.decisions:
        click.echo(f"  {d.symbol}: {d.action} ({d.reason})")


@main.command("daily-report")
def daily_report() -> None:
    """Email the daily P&L summary."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)

    import datetime as _dt_dr
    account = alpaca.get_account()
    try:
        regime_dr = _live_regime(market, cfg).regime.value
    except Exception:
        regime_dr = "unknown"

    _ctx_dr = DigestContext(
        date=_dt_dr.date.today(),
        starting_equity=account.equity,
        ending_equity=account.equity,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime=regime_dr,
        active_config_version="unknown",
    )
    _email_dr = build_daily_digest_email(_ctx_dr)

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject="Trading Bot — Daily Report", html_body=_email_dr.html_body,
                kind="digest", recipient=cfg.email.to)
    click.echo(f"Sent daily report to {cfg.email.to}")


@main.command("daily-digest")
def daily_digest() -> None:
    """Email the rebuilt 12-section daily digest (B3)."""
    import datetime as _dt
    import json as _json
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    today = _dt.date.today()

    account = alpaca.get_account()

    # equity_30d — try state.db equity_high_water_mark table; fallback to empty
    equity_30d: list[Decimal] = []
    try:
        from trading_bot.state_db import get_engine as _get_engine
        from sqlalchemy import text as _text
        _db_path = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
        _engine = _get_engine(_db_path)
        with _engine.connect() as _conn:
            rows = _conn.execute(
                _text("SELECT equity FROM equity_high_water_mark ORDER BY recorded_at DESC LIMIT 30")
            ).fetchall()
        equity_30d = [Decimal(str(r[0])) for r in reversed(rows)]
    except Exception:
        pass  # table may not exist yet — degrade gracefully

    # pending_promotions — from LabPromotionStore
    pending_promotions: list[dict] = []
    try:
        from trading_bot.lab_promotions import LabPromotionStore as _LPS
        import datetime as _dt2
        _now = _dt2.datetime.now(_dt2.timezone.utc)
        pending_promotions = [
            p if isinstance(p, dict) else p.__dict__
            for p in _LPS().pending_validation(now=_now)
        ]
    except Exception:
        pass  # TODO: wire once LabPromotionStore.pending_validation is stable

    # closed_trades_7d — from ClosedTradeStore filtered to last 7d
    closed_trades_7d: list[dict] = []
    try:
        import datetime as _dt3
        _cutoff = _dt3.datetime.now(_dt3.timezone.utc) - _dt3.timedelta(days=7)
        _all_closed = ClosedTradeStore(CLOSED_DB_PATH).all()
        for _ct in _all_closed:
            _closed_at = getattr(_ct, "closed_at", None)
            if _closed_at and _closed_at >= _cutoff:
                closed_trades_7d.append(
                    _ct if isinstance(_ct, dict) else _ct.__dict__
                )
    except Exception:
        pass  # TODO: enrich once ClosedTradeStore.all() is stable

    # schedule_audit_warnings — from ScheduleAuditStore
    schedule_audit_warnings: list[dict] = []
    try:
        from trading_bot.schedule_audit import ScheduleAuditStore as _SAS
        _rows = _SAS().latest(audit_date=today)
        for _r in _rows:
            _ratio = getattr(_r, "ratio", 1.0)
            if _ratio < 0.5:
                schedule_audit_warnings.append(
                    _r if isinstance(_r, dict) else _r.__dict__
                )
    except Exception:
        pass  # TODO: wire once ScheduleAuditStore has data

    # emails_sent_by_kind — from EmailLogStore
    emails_sent_by_kind: dict[str, int] = {}
    try:
        import datetime as _dt4
        from trading_bot.email_log import EmailLogStore as _ELS
        _today_start = _dt4.datetime.combine(today, _dt4.time.min).replace(
            tzinfo=_dt4.timezone.utc
        )
        emails_sent_by_kind = _ELS().count_by_kind_since(_today_start)
    except Exception:
        pass  # TODO: enrich once EmailLogStore is available

    # git_sha + version from paper_active.json
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

    # regime
    market = MarketDataClient(settings)
    try:
        regime = _live_regime(market, cfg).regime.value
    except Exception:
        regime = "unknown"

    ctx = DigestContext(
        date=today,
        starting_equity=account.equity,   # TODO: use yesterday's closing equity
        ending_equity=account.equity,
        realized_pnl=Decimal("0"),         # TODO: wire from PnlStateBuilder
        unrealized_pnl=Decimal("0"),       # TODO: wire from PnlStateBuilder
        regime=regime,
        active_config_version=version,
        equity_30d=equity_30d,
        pending_promotions=pending_promotions,
        closed_trades_7d=closed_trades_7d,
        schedule_audit_warnings=schedule_audit_warnings,
        emails_sent_by_kind=emails_sent_by_kind,
        git_sha=git_sha,
        version=version,
        # TODO: vix, watchlist_movers, sentiment_scores, positions formatting
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
    from trading_bot.alpaca_client import AlpacaClient
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
    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
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

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
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
        import datetime as _dt_is
        account = alpaca.get_account()
        _ctx_is = DigestContext(
            date=_dt_is.date.today(),
            starting_equity=account.equity,
            ending_equity=account.equity,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            regime=regime,
            active_config_version="unknown",
        )
        _email_is = build_daily_digest_email(_ctx_is)
        sender = EmailSender(
            user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
        )
        send_logged(sender=sender,
                    subject=f"Trading Bot — Intel Scan ({len(placed)} placed)",
                    html_body=_email_is.html_body, kind="alert", recipient=cfg.email.to)
        click.echo(f"[email] sent (action taken)")


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
        html = build_alert_email_html(events, account_equity=curr.equity)
        sender = EmailSender(
            user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
        )
        send_logged(sender=sender, subject="Trading Bot — Portfolio Alert", html_body=html,
                    kind="alert", recipient=cfg.email.to)
        click.echo("[email] alert sent")


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
    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
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
    is irrelevant. Silent unless an order is placed or rejected."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)
    market = MarketDataClient(settings)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    watchlist = _load_active_universe(crypto_only=True)
    if not watchlist:
        click.echo("[crypto-scan] empty crypto universe — nothing to scan")
        return

    pnl_builder = PnlStateBuilder(settings, cfg)
    regime_reading = _live_regime(market, cfg)
    regime = regime_reading.regime.value

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca,
        journal=journal, regime=regime,
        state_builder=pnl_builder.to_risk_state,
    )
    result = orch.scan(watchlist=watchlist)
    write_last_scan(command="crypto-scan", regime=regime, universe_size=len(watchlist), result=result)
    placed = [d for d in result.decisions if d.action == "placed_order"]
    rejected = [d for d in result.decisions if d.action == "rejected_by_risk"]

    click.echo(f"[crypto-scan] regime={regime} symbols={len(watchlist)} "
               f"placed={len(placed)} rejected={len(rejected)}")
    for d in placed:
        click.echo(f"  PLACED {d.symbol}: {d.reason} (entry={d.entry_order_id})")
    for d in rejected:
        click.echo(f"  REJECTED {d.symbol}: {d.reason}")


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

    from trading_bot.alpaca_client import AlpacaClient
    from trading_bot.market_data import MarketDataClient
    from trading_bot.position_protection import evaluate_and_act
    from trading_bot.supervisor import _is_market_hours_et

    settings = Settings()
    cfg = load_config(CONFIG_PATH)

    try:
        alpaca = AlpacaClient(settings)
        positions = alpaca.get_positions()
        open_orders = alpaca._client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
        )
    except Exception as e:
        click.echo(f"[verify-stops] alpaca query failed: {e}")
        return  # do not raise SystemExit — would kill the APScheduler worker.

    def _canon(sym: str) -> str:
        return str(sym).replace("/", "").upper()

    stops_by_symbol: dict[str, list] = {}
    for o in open_orders:
        if str(getattr(o, "type", "")).lower().endswith(("stop", "stop_limit")):
            stops_by_symbol.setdefault(_canon(o.symbol), []).append(o)

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

    html = build_open_positions_email_html(
        actions, total_positions=len(positions)
    )
    subject = open_positions_email_subject(actions)
    sender = EmailSender(
        user=settings.gmail_user,
        app_password=settings.gmail_app_password,
        to=cfg.email.to,
    )
    send_logged(sender=sender, subject=subject, html_body=html,
                kind="alert", recipient=cfg.email.to)
    click.echo(f"[verify-stops] summary email sent to {cfg.email.to}")


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

    html = build_vip_alert_email_html(high)
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    send_logged(sender=sender, subject=f"VIP TWEET ALERT — {len(high)} high-severity",
                html_body=html, kind="alert", recipient=cfg.email.to)
    click.echo(f"[vip-scan] alert email sent to {cfg.email.to}")


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
    from trading_bot.daemon import main as daemon_main
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


@main.command("midday-snapshot")
def midday_snapshot_cli() -> None:
    """Build + send the midday snapshot email at 12:00 ET."""
    import datetime as _dt_ms
    import json as _json_ms
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    alpaca = AlpacaClient(settings)

    account = alpaca.get_account()

    # TODO: wire regime from live detect_regime once reliable
    try:
        market = MarketDataClient(settings)
        regime = _live_regime(market, cfg).regime.value
    except Exception:
        regime = "unknown"

    # git_sha + version from paper_active.json
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

    from trading_bot.email_midday import SnapshotContext, build_midday_snapshot_email
    ctx = SnapshotContext(
        as_of=_dt_ms.datetime.now(_dt_ms.timezone.utc),
        equity=account.equity,
        starting_equity=account.equity,   # TODO: use today's open equity
        realized_pnl_today=Decimal("0"),  # TODO: wire from PnlStateBuilder
        unrealized_pnl=Decimal("0"),      # TODO: wire from PnlStateBuilder
        regime=regime,
        positions=[],                     # TODO: format alpaca positions
        trades_today=[],                  # TODO: wire from TradeJournal
        watchlist_signals=[],             # TODO: wire from screener near-miss list
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
