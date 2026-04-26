from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import click

from trading_bot.alpaca_client import AlpacaClient, AssetClass, OrderRequest, OrderSide
from trading_bot.config import Settings, load_config
from trading_bot.email_sender import EmailSender
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
    build_daily_report_html,
    build_rich_report_html,
)
from trading_bot.risk_manager import RiskManager, RiskState
from trading_bot.state import load_watchlist
from trading_bot.trade_journal import TradeJournal
from trading_bot.screener import build_stage1_shortlist, run_stage2, write_opportunities_snapshot
from trading_bot.strategy_lanes import BreakoutLane, MeanReversionLane, MomentumLane
from trading_bot.universe import build_universe, write_universe_snapshot

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

    rows = "".join(
        f"<tr><td>{p.symbol}</td><td>{p.qty}</td><td>${p.market_value}</td>"
        f"<td>${p.unrealized_pl}</td></tr>"
        for p in positions
    ) or "<tr><td colspan='4'><i>No open positions</i></td></tr>"

    html = f"""
<h2>Trading Bot — Account Status</h2>
<p>Generated {datetime.now().isoformat(timespec='seconds')}</p>
<table border='1' cellpadding='6'>
  <tr><th>Equity</th><td>${account.equity}</td></tr>
  <tr><th>Cash</th><td>${account.cash}</td></tr>
  <tr><th>Buying Power</th><td>${account.buying_power}</td></tr>
  <tr><th>Portfolio Value</th><td>${account.portfolio_value}</td></tr>
</table>
<h3>Open Positions</h3>
<table border='1' cellpadding='6'>
  <tr><th>Symbol</th><th>Qty</th><th>Market Value</th><th>Unrealized P&amp;L</th></tr>
  {rows}
</table>
"""

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    sender.send(subject="Trading Bot — Status", html_body=html)
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

    account = alpaca.get_account()
    positions = alpaca.get_positions()

    try:
        bars = market.get_daily_bars("SPY", lookback_days=2)
        if len(bars) >= 2:
            yesterday, today = bars["close"].iloc[-2], bars["close"].iloc[-1]
            spy_change = Decimal(str((today / yesterday - 1.0) * 100)).quantize(Decimal("0.01"))
        else:
            spy_change = Decimal("0.00")
    except Exception:
        spy_change = Decimal("0.00")

    empty_scan = ScanResult(decisions=[], timestamp=datetime.now())
    html = build_daily_report_html(
        account=account, positions=positions, scan=empty_scan,
        spy_daily_change_pct=spy_change, regime="trending_up",
    )

    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    sender.send(subject="Trading Bot — Daily Report", html_body=html)
    click.echo(f"Sent daily report to {cfg.email.to}")


@main.command()
def reconcile() -> None:
    """Match journal entries to Alpaca closed orders; populate closed_trades."""
    settings = Settings()
    cfg = load_config(CONFIG_PATH)
    journal = TradeJournal(Path(cfg.storage.trade_journal_path))
    closed = ClosedTradeStore(CLOSED_DB_PATH)
    rec = Reconciler(settings, journal, closed)
    summary = rec.reconcile(lookback_days=30)
    click.echo(f"Reconcile complete: {summary.new_closed} new closed trades recorded.")


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

    # 4. Email daily report
    account = alpaca.get_account()
    positions = alpaca.get_positions()
    try:
        bars = market.get_daily_bars("SPY", lookback_days=2)
        if len(bars) >= 2:
            yesterday, today = bars["close"].iloc[-2], bars["close"].iloc[-1]
            spy_change = Decimal(str((today / yesterday - 1.0) * 100)).quantize(Decimal("0.01"))
        else:
            spy_change = Decimal("0.00")
    except Exception:
        spy_change = Decimal("0.00")

    html = build_daily_report_html(
        account=account, positions=positions, scan=result,
        spy_daily_change_pct=spy_change, regime=regime,
    )
    sender = EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    )
    sender.send(subject=f"Trading Bot — Daily Report ({regime})", html_body=html)
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
        account = alpaca.get_account()
        positions = alpaca.get_positions()
        try:
            bars = market.get_daily_bars("SPY", lookback_days=2)
            spy_change = (Decimal(str((bars["close"].iloc[-1] / bars["close"].iloc[-2] - 1) * 100))
                          .quantize(Decimal("0.01")) if len(bars) >= 2 else Decimal("0"))
        except Exception:
            spy_change = Decimal("0")
        html = build_daily_report_html(
            account=account, positions=positions, scan=result,
            spy_daily_change_pct=spy_change, regime=regime,
        )
        EmailSender(
            user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
        ).send(subject=f"Trading Bot — Intel Scan ({len(placed)} placed)", html_body=html)
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
        EmailSender(
            user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
        ).send(subject="Trading Bot — Portfolio Alert", html_body=html)
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

    account = alpaca.get_account()
    positions = alpaca.get_positions()

    html = build_rich_report_html(
        period=period, account=account, positions=positions, scan=result,
        spy_daily_change_pct=spy_change, regime=regime, intel=intel, events=events,
    )
    EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    ).send(subject=f"Trading Bot — {period.upper()} Rich Report ({regime})", html_body=html)
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

    account = alpaca.get_account()
    positions = alpaca.get_positions()
    empty_scan = ScanResult(decisions=[], timestamp=datetime.now())

    html = build_rich_report_html(
        period="eod", account=account, positions=positions, scan=empty_scan,
        spy_daily_change_pct=spy_change, regime=regime, intel=intel, events=events,
    )
    EmailSender(
        user=settings.gmail_user, app_password=settings.gmail_app_password, to=cfg.email.to
    ).send(subject=f"Trading Bot — EOD Report ({regime})", html_body=html)
    click.echo(f"[eod-report] sent to {cfg.email.to} (regime={regime}, "
               f"VIX={intel.macro.vix}, {len(events)} events)")


@main.command("screen-universe")
def screen_universe() -> None:
    """Pull Alpaca tradable universe, apply liquidity screen, write snapshot."""
    settings = Settings()
    market = MarketDataClient(settings)

    def bar_loader(symbol: str):
        try:
            return market.get_daily_bars(symbol, lookback_days=20)
        except Exception:
            import pandas as pd
            return pd.DataFrame()

    alpaca = AlpacaClient(settings)
    assets = build_universe(alpaca, bar_loader=bar_loader)
    write_universe_snapshot(
        assets,
        Path("strategy/latest_intelligence.md"),
        generated_at=datetime.now(timezone.utc),
    )
    click.echo(f"Wrote universe snapshot: {len(assets)} liquid assets")


@main.command("dashboard")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code change (dev).")
def dashboard(host: str, port: int, reload: bool) -> None:
    """Start the local trading-bot dashboard at http://HOST:PORT (default 127.0.0.1:8765)."""
    from trading_bot.dashboard.app import run
    click.echo(f"Dashboard starting at http://{host}:{port} — Ctrl+C to stop")
    run(host=host, port=port, reload=reload)


@main.command("rank")
def rank_command() -> None:
    """Run stage-1 + stage-2 screener; write strategy/opportunities.md."""
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

    universe = build_universe(alpaca, bar_loader=bar_loader_short)
    shortlist = build_stage1_shortlist(universe, bar_loader=bar_loader_short, top_n=100)

    lanes = [MomentumLane(), MeanReversionLane(), BreakoutLane()]
    result = run_stage2(shortlist, lanes=lanes, bar_loader=bar_loader_long)
    write_opportunities_snapshot(
        result,
        Path("strategy/opportunities.md"),
        generated_at=datetime.now(timezone.utc),
        shortlist=shortlist,
    )
    click.echo(f"Stage-2 ranked {len(result.candidates)} candidates across {len(lanes)} lanes")


if __name__ == "__main__":
    main()
