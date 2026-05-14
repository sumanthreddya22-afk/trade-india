"""`bot` — the single CLI entry-point for v4.

Subcommands:
  bot daemon         — run the long-lived scheduler
  bot dashboard      — serve the operator UI on localhost
  bot status         — print one-shot status (no daemon required)
  bot halt           — fire the manual kill switch
  bot resume         — clear the manual kill switch
  bot risk-profile   — show or set safe / neutral / aggressive
  bot strategy       — list registered strategies / submit a new hypothesis
  bot verify         — run boot checks + chain verify (read-only)
  bot version        — print git SHA + python version

This file is intentionally thin: every subcommand delegates to a module
function so the dashboard and CLI share one implementation.
"""
from __future__ import annotations

import json
import os
import sys
import logging
from pathlib import Path

import click


@click.group(help="Trading-bot v4 operator CLI.")
@click.version_option(version="0.4.0", prog_name="bot")
def main() -> None:
    """Entry-point invoked by the `bot` console script."""
    pass


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
@main.command(help="Run the long-lived scheduler (foreground; SIGTERM-aware).")
@click.option("--once", is_flag=True, default=False,
              help="Tick every job once and exit (for smoke tests).")
@click.option("--no-broker", is_flag=True, default=False,
              help="Skip Alpaca adapter (useful for local dev without creds).")
def daemon(once: bool, no_broker: bool) -> None:
    from trading_bot.daemon import DaemonConfig, run_daemon
    from trading_bot.daemon.jobs import DaemonContext

    ctx = DaemonContext()

    if not no_broker:
        try:
            from trading_bot.ingest.alpaca_adapter import AlpacaAdapter
            adapter = AlpacaAdapter()
            ctx.broker_submit = adapter.submit_order
            ctx.positions_fetcher = adapter.fetch_positions
            ctx.bars_fetcher = adapter.fetch_latest_bars
            ctx.account_fetcher = adapter.fetch_account
            # Stash adapter on the context so orphan_loop can use the
            # lookup method (jobs.py reads ``_broker_adapter``).
            object.__setattr__(ctx, "_broker_adapter", adapter)
            click.echo("daemon: Alpaca adapter wired (paper)", err=True)
        except Exception as e:  # noqa: BLE001
            click.echo(f"daemon: Alpaca adapter unavailable ({e}); running headless", err=True)

    # Universe from .env or default to the seed-thesis 10-ETF list.
    universe = os.environ.get("TRADING_BOT_UNIVERSE", "")
    if universe:
        ctx.universe = tuple(s.strip() for s in universe.split(",") if s.strip())
    else:
        ctx.universe = ("SPY", "QQQ", "IWM", "DIA", "EFA", "EEM",
                        "XLK", "XLF", "XLE", "XLV")

    rc = run_daemon(ctx=ctx, config=DaemonConfig(), once=once)
    sys.exit(rc)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@main.command(help="Serve the operator dashboard on http://localhost:<port>.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--reload", is_flag=True, default=False)
def dashboard(host: str, port: int, reload: bool) -> None:
    import uvicorn
    uvicorn.run(
        "trading_bot.operator_ui.app:app",
        host=host, port=port, reload=reload, log_level="info",
    )


# ---------------------------------------------------------------------------
# Status / halt / resume / risk-profile / strategy
# Thin wrappers around trading_bot.operator.* functions.
# ---------------------------------------------------------------------------
@main.command(help="Print one-shot system status (JSON).")
def status() -> None:
    from trading_bot.operator import controls
    click.echo(json.dumps(controls.status_snapshot(), indent=2, default=str))


@main.command(help="Fire the manual halt kill switch.")
@click.option("--reason", required=True, help="Audit reason (logged).")
def halt(reason: str) -> None:
    from trading_bot.operator import controls
    out = controls.halt(reason=reason, operator=os.environ.get("USER", "operator"))
    click.echo(json.dumps(out, indent=2, default=str))


@main.command(help="Clear the manual halt kill switch.")
@click.option("--reason", required=True, help="Audit reason (logged).")
def resume(reason: str) -> None:
    from trading_bot.operator import controls
    out = controls.resume(reason=reason, operator=os.environ.get("USER", "operator"))
    click.echo(json.dumps(out, indent=2, default=str))


@main.command("risk-profile", help="Show or set the risk profile.")
@click.argument("profile", required=False,
                type=click.Choice(["safe", "neutral", "aggressive", "show"]))
def risk_profile(profile: str | None) -> None:
    from trading_bot.operator import controls
    if not profile or profile == "show":
        click.echo(json.dumps(controls.risk_profile_show(), indent=2, default=str))
        return
    out = controls.risk_profile_set(
        profile, operator=os.environ.get("USER", "operator"),
    )
    click.echo(json.dumps(out, indent=2, default=str))


@main.group(help="Strategy registry operations.")
def strategy() -> None:
    pass


@strategy.command("list", help="List registered strategy versions.")
def strategy_list() -> None:
    from trading_bot.operator import controls
    click.echo(json.dumps(controls.strategy_list(), indent=2, default=str))


@strategy.command("promote", help="Promote a strategy to a new lane status.")
@click.option("--id", "strategy_id", required=True, help="strategy_id (e.g. ETF_MOMENTUM_v1)")
@click.option("--to", "target_status", required=True,
              type=click.Choice(["shadow", "tiny_paper", "scaled_paper", "live"]))
@click.option("--artifact", "artifact_id", default=None,
              help="validation_artifact_id (auto-resolved from latest pass if omitted)")
@click.option("--packet", "packet_id", default=None,
              help="promotion_packet_id (required for live target)")
def strategy_promote(strategy_id: str, target_status: str,
                     artifact_id: str | None, packet_id: str | None) -> None:
    from trading_bot.operator import controls
    out = controls.strategy_promote(
        strategy_id=strategy_id, target_status=target_status,
        artifact_id=artifact_id, packet_id=packet_id,
        operator=os.environ.get("USER", "operator"),
    )
    click.echo(json.dumps(out, indent=2, default=str))


@strategy.command("submit", help="Submit a natural-language strategy hypothesis.")
@click.option("--name", required=True, help="Short strategy name (e.g. MEAN_REV_v1).")
@click.option("--description", required=True,
              help="Plain-English hypothesis. Wrap in quotes.")
@click.option("--mode", default="draft", show_default=True,
              type=click.Choice(["draft", "intake", "mutate"]),
              help="'draft'=register only; 'intake'=adversarial pair; 'mutate'=mutation cycle.")
def strategy_submit(name: str, description: str, mode: str) -> None:
    from trading_bot.operator import controls
    out = controls.strategy_submit(
        name=name, description=description, mode=mode,
        operator=os.environ.get("USER", "operator"),
    )
    click.echo(json.dumps(out, indent=2, default=str))


# ---------------------------------------------------------------------------
# Verify / version
# ---------------------------------------------------------------------------
@main.command(help="Run boot checks + ledger chain verify (read-only).")
def verify() -> None:
    from trading_bot.kernel.boot import run_boot_checks
    report = run_boot_checks()
    click.echo(json.dumps({
        "ok": report.ok,
        "checks": report.checks,
        "active_kills": report.active_kills,
    }, indent=2, default=str))
    sys.exit(0 if report.ok else 2)


@main.command(help="Print a digest of the last N hours (default 24).")
@click.option("--hours", default=24, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def digest(hours: int, as_json: bool) -> None:
    from trading_bot.operator.digest import build_digest, format_digest_text
    d = build_digest(hours=hours)
    if as_json:
        click.echo(json.dumps(d, indent=2, default=str))
    else:
        click.echo(format_digest_text(d))


@main.command(help="Print version info.")
def version() -> None:
    import platform
    click.echo(json.dumps({
        "bot_version": "0.4.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
    }, indent=2))


if __name__ == "__main__":
    main()
