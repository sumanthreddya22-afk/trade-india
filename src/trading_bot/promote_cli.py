"""bot promote CLI — manual gate for graduating paper → live.

Spec §12 is unambiguous: live trading activation is a deliberate, conscious
decision the operator makes. This module implements three layers of refusal
for the live target so no flag, env, or alias can shortcut it.

Paper target: replicates the lab Promoter's logic on demand. Reversible.
Live target: requires (a) live API creds, (b) explicit flag, (c) typed
confirmation string. ALL THREE OR REFUSE.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import click

# The literal string the operator must type. NO regex. NO substring. NO case-insensitive.
LIVE_CONFIRM_STRING = "YES, FLIP TO LIVE"

# Locked stricter risk caps for live mode (half of paper).
LOCKED_LIVE_RISK_CAPS = {
    "max_position_pct": 5,
    "daily_loss_pct": 1.5,
    "max_drawdown_pct": 10,
}


class PromoteRefused(Exception):
    """Raised internally when a guardrail check fails. Caller maps to exit 1."""


def promote_to_paper(
    *,
    state_db_path: Path,
    active_path: Path,
    leaderboard_id: int | None = None,
    notify: bool = False,
) -> dict:
    """Replicate the lab Promoter's logic against current state. Always reversible.

    `notify=False` (default) means tests and dry-runs don't write to
    production state.db's lab_promotions table or send a real email. The
    `bot promote` CLI command flips it to True for the live invocation.
    """
    from sqlalchemy.orm import Session

    from trading_bot.leaderboard import current_best
    from trading_bot.promotion import (
        PromotionCandidate,
        promote_atomically,
        should_promote,
    )
    from trading_bot.state_db import Leaderboard, get_engine

    engine = get_engine(state_db_path)
    with Session(engine) as session:
        if leaderboard_id is not None:
            row = session.get(Leaderboard, leaderboard_id)
            if row is None:
                raise PromoteRefused(f"leaderboard id {leaderboard_id} not found")
        else:
            row = current_best(session)
            if row is None:
                raise PromoteRefused("leaderboard is empty")

        params = json.loads(row.params_json)
        candidate = PromotionCandidate(
            template=row.template_name,
            params=params,
            fitness=row.fitness_score,
            alpha_vs_spy_x=row.alpha_vs_spy_x,
            sortino=row.sortino,
            max_dd_pct=row.max_dd_pct,
        )

    ok, info = should_promote(active_path, candidate)
    if not ok:
        return {"promoted": False, "reason": info.get("reason"), "info": info}
    promote_atomically(active_path, candidate, notify=notify)
    return {
        "promoted": True,
        "to_template": candidate.template,
        "to_fitness": candidate.fitness,
        "info": info,
    }


def promote_to_live(
    *,
    paper_active_path: Path,
    live_active_path: Path,
    state_db_path: Path,
    i_know_real_money: bool,
    confirm_input_provider=input,
) -> dict:
    """The live promotion gate. Three independent checks; ALL must pass.

    Returns dict on success. Raises PromoteRefused on any guardrail violation.
    """
    # Gate 1: live API creds in environment.
    api_key = os.environ.get("ALPACA_LIVE_API_KEY")
    api_secret = os.environ.get("ALPACA_LIVE_API_SECRET")
    if not api_key or not api_secret:
        raise PromoteRefused(
            "ALPACA_LIVE_API_KEY and ALPACA_LIVE_API_SECRET must both be set "
            "in the environment before promoting to live."
        )

    # Gate 2: explicit flag.
    if not i_know_real_money:
        raise PromoteRefused(
            "--i-know-this-is-real-money flag required to promote to live."
        )

    # Gate 3: source paper config exists.
    if not paper_active_path.exists():
        raise PromoteRefused(
            f"paper_active.json missing at {paper_active_path}. "
            "Live config is initialized from paper — paper must exist first."
        )

    # Print the dramatic banner with the proposed change.
    paper_cfg = json.loads(paper_active_path.read_text())
    _print_live_banner(paper_cfg, live_active_path)

    # Gate 4: typed confirmation. No regex, no substring, exact match only.
    response = confirm_input_provider(
        f'\nType "{LIVE_CONFIRM_STRING}" to proceed (any other input cancels): '
    )
    if response != LIVE_CONFIRM_STRING:
        raise PromoteRefused(
            f"confirmation string did not match (got {response!r}). Live "
            "promotion cancelled."
        )

    # All gates passed. Build live config from paper with overrides.
    live_cfg = dict(paper_cfg)
    live_cfg["bot_mode"] = "live"
    live_cfg["risk_caps"] = LOCKED_LIVE_RISK_CAPS
    live_cfg["promoted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    live_cfg["promoted_by"] = "bot-promote-cli"
    live_cfg["version"] = (
        f"live-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    )

    # Atomic write
    live_active_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = live_active_path.with_suffix(live_active_path.suffix + ".tmp")
    tmp.write_text(json.dumps(live_cfg, indent=2, sort_keys=True))
    os.replace(tmp, live_active_path)

    # Record in ConfigHistory for audit
    _record_config_history(state_db_path, payload=live_cfg)

    return {
        "promoted": True,
        "live_active_path": str(live_active_path),
        "version": live_cfg["version"],
        "risk_caps_applied": LOCKED_LIVE_RISK_CAPS,
    }


def _print_live_banner(paper_cfg: dict, live_path: Path) -> None:
    click.echo()
    click.echo("=" * 70)
    click.echo(click.style("  LIVE TRADING PROMOTION", fg="red", bold=True))
    click.echo("=" * 70)
    click.echo()
    click.echo("This will create a LIVE config at:")
    click.echo(f"  {live_path}")
    click.echo()
    click.echo("Source paper config:")
    click.echo(f"  template:       {paper_cfg.get('active_template', 'n/a')}")
    click.echo(f"  fitness:        {paper_cfg.get('fitness_at_promotion', 'n/a')}")
    click.echo(f"  promoted_at:    {paper_cfg.get('promoted_at', 'n/a')}")
    click.echo()
    click.echo("Live mode will apply these LOCKED stricter caps:")
    for k, v in LOCKED_LIVE_RISK_CAPS.items():
        click.echo(f"  {k:<20s} {v}")
    click.echo()
    click.echo("Real money will be at risk.")
    click.echo("=" * 70)


def _record_config_history(state_db_path: Path, *, payload: dict) -> None:
    from sqlalchemy.orm import Session

    from trading_bot.state_db import ConfigHistory, get_engine

    try:
        engine = get_engine(state_db_path)
        with Session(engine) as session:
            session.add(
                ConfigHistory(
                    account="live",
                    version=payload.get("version", "unknown"),
                    git_sha=None,
                    promoted_at=dt.datetime.now(dt.timezone.utc),
                    promoted_by="bot-promote-cli",
                    payload_json=json.dumps(payload, sort_keys=True),
                )
            )
            session.commit()
    except Exception:
        # Don't fail the promotion just because we couldn't audit-log;
        # the file write is the load-bearing operation.
        pass


def register_promote_command(main_group: Any) -> None:
    """Attach the `bot promote` subcommand to the existing click group."""

    @main_group.command("promote")
    @click.option(
        "--target",
        type=click.Choice(["paper", "live"]),
        required=True,
        help="paper: replicates lab Promoter on demand. live: GATED — see help.",
    )
    @click.option(
        "--leaderboard-id",
        type=int,
        default=None,
        help="(paper only) Promote a specific leaderboard row id, not the top.",
    )
    @click.option(
        "--i-know-this-is-real-money",
        is_flag=True,
        default=False,
        help="REQUIRED for --target=live. Affirms operator awareness.",
    )
    @click.option(
        "--paper-active",
        type=str,
        default="data/paper_active.json",
        show_default=True,
    )
    @click.option(
        "--live-active",
        type=str,
        default="data/live_active.json",
        show_default=True,
    )
    @click.option(
        "--state-db",
        type=str,
        default="data/state.db",
        show_default=True,
    )
    def promote_cmd(
        target: str,
        leaderboard_id: int | None,
        i_know_this_is_real_money: bool,
        paper_active: str,
        live_active: str,
        state_db: str,
    ) -> None:
        """Promote a config — paper (reversible) or live (gated, irreversible)."""
        try:
            if target == "paper":
                result = promote_to_paper(
                    state_db_path=Path(state_db),
                    active_path=Path(paper_active),
                    leaderboard_id=leaderboard_id,
                    notify=True,
                )
                click.echo(json.dumps(result, indent=2, default=str))
                if not result["promoted"]:
                    raise SystemExit(1)
            else:
                result = promote_to_live(
                    paper_active_path=Path(paper_active),
                    live_active_path=Path(live_active),
                    state_db_path=Path(state_db),
                    i_know_real_money=i_know_this_is_real_money,
                )
                click.echo()
                click.echo(click.style("Live config written.", fg="green", bold=True))
                click.echo(json.dumps(result, indent=2, default=str))
                click.echo()
                click.echo(
                    "Next: run ops/install_live.sh to load the live daemon "
                    "(requires a second typed confirmation)."
                )
        except PromoteRefused as e:
            click.echo(click.style(f"REFUSED: {e}", fg="red", bold=True), err=True)
            raise SystemExit(1)
