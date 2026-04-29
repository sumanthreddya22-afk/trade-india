"""Self-evolution: analyze closed trade performance and propose rule changes.

The bot reviews its own track record (per strategy, regime, symbol) and
emits structured proposals. By default proposals are written to
`strategy/rules.md` evolution log for human review. With `--apply` they
also update the active parameter values stored in `strategy/params.yaml`.

**Hard guardrails (never auto-changed):**
- Daily/weekly loss circuit breakers
- Max position size, concentration cap
- Stop-loss mandatory
- Paper-only enforcement

These live in `strategy/config.yaml` and are loaded by `RiskManager`. The
evolution loop only ever writes to `strategy/params.yaml` (soft params)
and the rules.md log.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from trading_bot.reconciliation import ClosedTrade, ClosedTradeStore


@dataclass(frozen=True)
class StrategyStats:
    strategy: str
    n_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    total_pnl: float
    avg_hold_hours: float


@dataclass(frozen=True)
class Proposal:
    description: str
    rationale: str
    parameter: str  # e.g., "momentum.rsi_lower"
    current_value: Any
    suggested_value: Any
    confidence: str  # "low" | "medium" | "high"


def evaluate_performance(closed: list[ClosedTrade], min_trades: int = 5) -> dict[str, StrategyStats]:
    """Compute per-strategy statistics from closed trades."""
    out: dict[str, StrategyStats] = {}
    by_strat: dict[str, list[ClosedTrade]] = {}
    for t in closed:
        by_strat.setdefault(t.strategy, []).append(t)

    for strategy, trades in by_strat.items():
        if len(trades) < min_trades:
            continue
        wins = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        win_rate = len(wins) / len(trades) if trades else 0.0
        avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0.0
        gross_profit = sum(float(t.realized_pnl) for t in wins)
        gross_loss = abs(sum(float(t.realized_pnl) for t in losses)) or 1.0
        profit_factor = gross_profit / gross_loss
        total_pnl = sum(float(t.realized_pnl) for t in trades)
        avg_hold = sum(t.hold_hours for t in trades) / len(trades)
        out[strategy] = StrategyStats(
            strategy=strategy, n_trades=len(trades), win_rate=win_rate,
            avg_win_pct=avg_win, avg_loss_pct=avg_loss,
            profit_factor=profit_factor, total_pnl=total_pnl, avg_hold_hours=avg_hold,
        )
    return out


# Default soft parameters (initial values match the strategy module defaults)
_DEFAULT_PARAMS: dict[str, Any] = {
    "momentum": {
        "rsi_lower": 55.0,
        "rsi_upper": 70.0,
        "per_trade_risk_pct": 0.5,
        "stop_pct": 0.05,
        "max_concentration_pct": 4.5,
    },
    "mean_reversion": {
        "rsi_lower": 25.0,
        "rsi_upper": 35.0,
        "per_trade_risk_pct": 0.5,
        "stop_pct": 0.04,
        "max_concentration_pct": 4.5,
    },
}


def load_params(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _DEFAULT_PARAMS.copy()
    raw = yaml.safe_load(path.read_text()) or {}
    # merge with defaults so missing keys take defaults
    merged = {k: {**v, **(raw.get(k, {}))} for k, v in _DEFAULT_PARAMS.items()}
    return merged


def save_params(path: Path, params: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(params, sort_keys=True))


def propose_rule_changes(
    stats: dict[str, StrategyStats],
    params: dict[str, Any],
) -> list[Proposal]:
    """Generate parameter-tweak proposals from observed performance.

    Logic (intentionally conservative):
    - Win rate < 40% on >= 20 trades → loosen RSI window slightly OR cut size
    - Win rate > 65% on >= 20 trades → consider scaling risk modestly
    - Avg loss exceeds avg win by 2x → tighten stop_pct
    """
    proposals: list[Proposal] = []
    for strategy, s in stats.items():
        p = params.get(strategy, {})
        if s.n_trades < 20:
            continue

        if s.win_rate < 0.40:
            cur_risk = p.get("per_trade_risk_pct", 0.5)
            new_risk = round(max(0.25, cur_risk * 0.5), 2)
            if new_risk != cur_risk:
                proposals.append(Proposal(
                    description=f"Reduce {strategy} per_trade_risk_pct to dampen losses",
                    rationale=f"win rate {s.win_rate:.0%} over {s.n_trades} trades is below 40%",
                    parameter=f"{strategy}.per_trade_risk_pct",
                    current_value=cur_risk,
                    suggested_value=new_risk,
                    confidence="medium",
                ))
        elif s.win_rate > 0.65 and s.profit_factor > 1.5:
            cur_risk = p.get("per_trade_risk_pct", 0.5)
            new_risk = round(min(1.0, cur_risk * 1.25), 2)
            if new_risk != cur_risk:
                proposals.append(Proposal(
                    description=f"Increase {strategy} per_trade_risk_pct (proven edge)",
                    rationale=f"win rate {s.win_rate:.0%}, profit factor {s.profit_factor:.2f} over {s.n_trades} trades",
                    parameter=f"{strategy}.per_trade_risk_pct",
                    current_value=cur_risk,
                    suggested_value=new_risk,
                    confidence="medium",
                ))

        if s.avg_loss_pct < 0 and abs(s.avg_loss_pct) > 2 * max(s.avg_win_pct, 0.1):
            cur_stop = p.get("stop_pct", 0.05)
            new_stop = round(max(0.03, cur_stop * 0.85), 3)
            if new_stop != cur_stop:
                proposals.append(Proposal(
                    description=f"Tighten {strategy} stop_pct to limit large losses",
                    rationale=f"avg loss {s.avg_loss_pct:.2f}% is more than 2x avg win {s.avg_win_pct:.2f}%",
                    parameter=f"{strategy}.stop_pct",
                    current_value=cur_stop,
                    suggested_value=new_stop,
                    confidence="medium",
                ))
    return proposals


def append_evolution_log(rules_md_path: Path, stats: dict[str, StrategyStats], proposals: list[Proposal], applied: bool) -> None:
    """Append a dated entry to the Evolution Log section of rules.md."""
    if not rules_md_path.exists():
        return
    body = rules_md_path.read_text()
    today = datetime.now(timezone.utc).date().isoformat()

    block = [f"\n### {today} — performance review"]
    if not stats:
        block.append("- No strategy has accumulated enough closed trades yet.")
    else:
        for s in stats.values():
            block.append(
                f"- **{s.strategy}**: {s.n_trades} trades, "
                f"win rate {s.win_rate:.0%}, profit factor {s.profit_factor:.2f}, "
                f"total P&L ${s.total_pnl:.2f}, avg hold {s.avg_hold_hours:.1f}h"
            )
    if proposals:
        block.append("")
        block.append("**Proposals:**" + (" (applied)" if applied else " (pending review)"))
        for pr in proposals:
            block.append(
                f"- {pr.description}: `{pr.parameter}` "
                f"`{pr.current_value}` → `{pr.suggested_value}` "
                f"({pr.confidence} confidence) — {pr.rationale}"
            )
    else:
        block.append("- No rule changes proposed.")

    rules_md_path.write_text(body.rstrip() + "\n" + "\n".join(block) + "\n")


def apply_proposals(params: dict[str, Any], proposals: list[Proposal]) -> dict[str, Any]:
    """Mutate a copy of the params dict with each proposal's suggested value."""
    new_params = {k: dict(v) for k, v in params.items()}
    for pr in proposals:
        strat, param = pr.parameter.split(".", 1)
        if strat in new_params:
            new_params[strat][param] = pr.suggested_value
    return new_params


# ─── Phase 5: wheel-aware analysis ─────────────────────────────────────────
import datetime as _dt
from decimal import Decimal as _Decimal
from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session
from trading_bot.state_db import WheelCycle as _WC


def report_wheel_kpis(engine: _Engine, *, lookback_days: int = 30) -> dict:
    """Aggregate closed-wheel-cycle metrics over the last N days.

    Returns ``{count, win_rate, avg_pnl, total_pnl}``. ``win_rate`` is the
    fraction of cycles whose realized_pnl is strictly > 0. Useful for the
    daily digest "Wheel" KPI block and the lab's evolution log.
    """
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=lookback_days)
    with _Session(engine) as s:
        rows = (
            s.query(_WC)
             .filter(_WC.phase == "closed", _WC.closed_at >= cutoff)
             .all()
        )
    count = len(rows)
    if count == 0:
        return {
            "count": 0, "win_rate": 0.0,
            "avg_pnl": _Decimal(0), "total_pnl": _Decimal(0),
        }
    wins = sum(1 for r in rows if (r.realized_pnl or _Decimal(0)) > 0)
    total = sum((r.realized_pnl or _Decimal(0) for r in rows), _Decimal(0))
    return {
        "count": count,
        "win_rate": wins / count,
        "avg_pnl": total / count,
        "total_pnl": total,
    }
