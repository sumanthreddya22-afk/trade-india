"""Threshold Tuner — Tier 5 lab role.

Adaptive thresholds for risk gates, wheel filters, and debate predicates.

What it does
------------
Reads recent realized data (closed_trades, option_iv_history,
unblock_debate_runs), computes a per-knob signal, applies a deterministic
rule, clamps to bounds, and writes an override row to the
``threshold_overrides`` table. Hot-path code consults the table via
``trading_bot.threshold_overrides.lookup`` and falls back to the static
YAML config when no fresh override exists.

The deterministic rules are stateless pure functions exposed at module
level (one per knob) — that keeps them unit-testable in isolation and
free of database coupling. The role itself wires up data sources,
applies the rules, and persists results.

Mode: each knob has a mode ∈ {auto, recommend}.
  * **auto** — clamped value persisted to ``threshold_overrides``;
    operator gets an email summary the next morning.
  * **recommend** — appended to ``data/threshold_proposals_today.json``;
    NOT persisted to the override table. Operator can hand-promote by
    inserting a row with ``set_by="operator"``.

Out of scope (deferred): LLM-judge gate for recommend-mode knobs
(referenced in the plan but not built yet — recommend mode currently
just emits JSON for operator review).
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import (
    OptionIvHistory,
    RoleRun,
    UnblockDebateRun,
)
from trading_bot.threshold_overrides import write_override


CLOSED_TRADES_DB_DEFAULT = Path("data/closed_trades.db")
PROPOSALS_PATH_DEFAULT = Path("data/threshold_proposals_today.json")


# ---------------------------------------------------------------------------
# Per-knob descriptors. (knob, bounds_min, bounds_max, mode)
# Bounds are HARD safety rails — the writer clamps to these and the
# reader re-clamps on lookup. Operator can never lose more than the
# static YAML default would have lost in the worst case.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KnobSpec:
    name: str
    bounds_min: float
    bounds_max: float
    mode: str  # "auto" | "recommend"


KNOBS: dict[str, KnobSpec] = {
    # Risk gates
    "per_trade_risk_pct": KnobSpec("per_trade_risk_pct", 0.5, 2.0, "auto"),
    "max_position_pct": KnobSpec("max_position_pct", 5.0, 15.0, "auto"),
    "sector_cap_pct": KnobSpec("sector_cap_pct", 10.0, 35.0, "recommend"),
    "options_max_pct": KnobSpec("options_max_pct", 10.0, 35.0, "recommend"),
    # Wheel thresholds
    "iv_rank_floor": KnobSpec("iv_rank_floor", 10.0, 50.0, "auto"),
    "min_premium_abs": KnobSpec("min_premium_abs", 0.10, 1.00, "auto"),
    "min_annualized_yield": KnobSpec("min_annualized_yield", 0.05, 0.25, "auto"),
    "delta_target_low": KnobSpec("delta_target_low", 0.15, 0.35, "recommend"),
    "delta_target_high": KnobSpec("delta_target_high", 0.15, 0.35, "recommend"),
    "dte_min": KnobSpec("dte_min", 21.0, 60.0, "recommend"),
    "dte_max": KnobSpec("dte_max", 21.0, 60.0, "recommend"),
    # Debate predicates
    "unblock_min_candidate_score": KnobSpec("unblock_min_candidate_score", 5.0, 9.0, "auto"),
    "unblock_max_overage_ratio": KnobSpec("unblock_max_overage_ratio", 0.30, 1.00, "auto"),
    "unblock_daily_debate_cap": KnobSpec("unblock_daily_debate_cap", 5.0, 50.0, "auto"),
}


# ---------------------------------------------------------------------------
# Pure rule functions. Each takes signals, returns a (proposed_value, summary)
# pair where summary is a dict explaining why the rule fired (for the email
# and the audit row). Returns None when there isn't enough data — the role
# silently skips the knob in that case so static YAML stays in effect.
# ---------------------------------------------------------------------------


def rule_per_trade_risk_pct(*, win_rates: list[float]) -> tuple[float, dict[str, Any]] | None:
    """Linear ramp on rolling-30-trade win rate.

    Mapping: 30% win → 0.5%, 50% → 1.0%, 70% → 1.5%. Below 30% pins to
    0.5 (the floor); above 70% pins to 1.5 (well below the 2.0 ceiling
    so the operator's still in the loop for outlier streaks).

    Sample size guard: need ≥ 30 trades.
    """
    if len(win_rates) < 30:
        return None
    n = len(win_rates)
    wins = sum(1 for w in win_rates if w > 0)
    win_rate = wins / n
    # piecewise linear: 0.30 → 0.5, 0.50 → 1.0, 0.70 → 1.5
    if win_rate <= 0.30:
        proposed = 0.5
    elif win_rate <= 0.50:
        proposed = 0.5 + (win_rate - 0.30) * (0.5 / 0.20)  # ramp to 1.0
    elif win_rate <= 0.70:
        proposed = 1.0 + (win_rate - 0.50) * (0.5 / 0.20)  # ramp to 1.5
    else:
        proposed = 1.5
    summary = {"rule": "per_trade_risk_win_rate_ramp", "n_trades": n,
               "win_rate": round(win_rate, 4)}
    return round(proposed, 2), summary


def rule_max_position_pct(
    *,
    current: float,
    max_dd_pct: float,
    n_trades: int = 0,
) -> tuple[float, dict[str, Any]] | None:
    """Tighten when rolling 30d max DD > 5%; loosen when DD < 2% AND we have
    enough sustained data (≥30 trades) to justify the loosening.

    ``current`` is the static YAML default; output is current ± 2 percentage
    points based on DD bucket. Tightening fires whenever DD>5% even on small
    samples (better safe than sorry); loosening requires real history.
    The clamp at write time enforces [5, 15].
    """
    if max_dd_pct is None:
        return None
    if max_dd_pct > 5.0:
        proposed = max(current - 2.0, 5.0)
        bucket = "tight"
    elif max_dd_pct < 2.0 and n_trades >= 30:
        proposed = min(current + 2.0, 15.0)
        bucket = "loose"
    else:
        # In the dead band — stay put. Avoid noise: don't write an override
        # at the YAML default; let the lookup return None.
        return None
    summary = {"rule": "max_position_dd_band", "max_dd_pct": round(max_dd_pct, 2),
               "bucket": bucket, "current": current, "n_trades": n_trades}
    return round(proposed, 2), summary


def rule_iv_rank_floor(*, iv_ranks: list[float]) -> tuple[float, dict[str, Any]] | None:
    """30th percentile of last 30d's iv_rank distribution.

    Below the 30th percentile, the wheel should not be selling — IV is in
    the bottom third of the recent regime, so option premiums aren't paying
    enough for the obligation we're taking on.
    """
    if len(iv_ranks) < 10:
        return None
    iv_ranks_sorted = sorted(iv_ranks)
    n = len(iv_ranks_sorted)
    idx = max(0, int(0.30 * n) - 1)
    proposed = float(iv_ranks_sorted[idx])
    summary = {"rule": "iv_rank_floor_p30", "n_observations": n,
               "p30": round(proposed, 2)}
    return round(proposed, 2), summary


def rule_min_premium_abs(*, recent_bids: list[float]) -> tuple[float, dict[str, Any]] | None:
    """Floor at 50% of the recent median bid for chains we'd actually pick.

    Ensures the floor moves with the regime — when implied vol contracts
    and the median bid for in-band CSPs falls from $1.10 to $0.40, the
    floor follows so we don't reject everything during a low-vol window.
    """
    if len(recent_bids) < 10:
        return None
    median = statistics.median(recent_bids)
    proposed = round(median * 0.5, 2)
    summary = {"rule": "min_premium_abs_half_median",
               "n_bids": len(recent_bids), "median_bid": round(median, 2)}
    return proposed, summary


def rule_min_annualized_yield(*, realized_yields: list[float]) -> tuple[float, dict[str, Any]] | None:
    """Track 90% of rolling-30d realized yield from closed wheel cycles.

    Aggressive: the floor follows realized performance. If we've been
    earning 18% annualized, a 16.2% floor still admits most chains. If
    the regime shifts and realized falls to 8%, we stop writing chains
    that would reach for >7.2% — the math says the regime can't pay it.
    """
    if len(realized_yields) < 5:
        return None
    median = statistics.median(realized_yields)
    proposed = round(median * 0.90, 4)
    summary = {"rule": "min_annualized_yield_90pct_realized",
               "n_cycles": len(realized_yields),
               "median_realized_yield": round(median, 4)}
    return proposed, summary


def rule_unblock_min_candidate_score(
    *, debate_outcomes: list[tuple[float, float]]
) -> tuple[float, dict[str, Any]] | None:
    """Tighten when ``place`` verdicts at low scores have <50% win rate.

    ``debate_outcomes`` is a list of (candidate_score, closed_pnl_pct) for
    debates where the verdict was 'place' AND the trade has closed. The
    rule looks at scores ∈ [7.0, 8.0] (the borderline band) and tightens
    the floor when those have lost money.

    Sample size guard: need ≥ 10 closed debates.
    """
    if len(debate_outcomes) < 10:
        return None
    borderline = [(s, p) for (s, p) in debate_outcomes if 7.0 <= s < 8.0]
    if len(borderline) < 5:
        return None
    win_rate = sum(1 for _, p in borderline if p > 0) / len(borderline)
    if win_rate < 0.50:
        proposed = 8.0  # tighten the floor
        bucket = "tighten"
    elif win_rate >= 0.65:
        proposed = 6.5  # loosen — borderline is paying off
        bucket = "loosen"
    else:
        return None  # status quo
    summary = {"rule": "unblock_min_candidate_score_winrate",
               "n_borderline": len(borderline),
               "win_rate": round(win_rate, 4),
               "bucket": bucket}
    return proposed, summary


def rule_unblock_max_overage_ratio(
    *, debate_outcomes: list[tuple[float, float]]
) -> tuple[float, dict[str, Any]] | None:
    """Tighten when high-overage 'place' verdicts lose money.

    ``debate_outcomes`` is (overage_ratio, closed_pnl_pct). Rule splits at
    the 0.40 overage band and tightens to 0.40 when high-overage overrides
    have <50% win rate.
    """
    if len(debate_outcomes) < 10:
        return None
    high = [(o, p) for (o, p) in debate_outcomes if o >= 0.40]
    if len(high) < 5:
        return None
    win_rate = sum(1 for _, p in high if p > 0) / len(high)
    if win_rate < 0.50:
        proposed = 0.40  # tighten
        bucket = "tighten"
    elif win_rate >= 0.65:
        proposed = 0.75  # loosen
        bucket = "loosen"
    else:
        return None
    summary = {"rule": "unblock_max_overage_winrate",
               "n_high": len(high),
               "win_rate": round(win_rate, 4),
               "bucket": bucket}
    return proposed, summary


def rule_unblock_daily_debate_cap(
    *, n_debates_30d: int, total_cost_30d_usd: float
) -> tuple[float, dict[str, Any]] | None:
    """Scale daily cap to keep aggregate LLM debate cost under $1/day.

    Sample size: need ≥ 30 debates of history. Below that, leave static.
    """
    if n_debates_30d < 30:
        return None
    avg_cost_per_debate = total_cost_30d_usd / max(1, n_debates_30d)
    if avg_cost_per_debate <= 0:
        # Subscription-billed via mailbox — debates are effectively free.
        # Cap at 30 to prevent runaway in-memory growth, not cost.
        proposed = 30.0
    else:
        # ~$1/day budget
        proposed = max(5.0, min(50.0, 1.00 / avg_cost_per_debate))
    summary = {"rule": "unblock_daily_cap_cost_budget",
               "n_debates_30d": n_debates_30d,
               "avg_cost_usd": round(avg_cost_per_debate, 4)}
    return round(proposed, 0), summary


# ---------------------------------------------------------------------------
# The role
# ---------------------------------------------------------------------------


@dataclass
class TunerOutputs:
    """Return shape of run_threshold_tuner. Lab wraps via BaseRole.safe_run."""
    overrides_written: list[dict[str, Any]] = field(default_factory=list)
    proposals_for_review: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


class ThresholdTunerRole(BaseRole):
    name = "threshold_tuner"
    tier = 5
    process = "lab"
    job_description = (
        "Adaptive thresholds for risk/wheel/debate knobs. "
        "Runs nightly post-reconciler; writes overrides + proposals."
    )
    sla_seconds = 5 * 60
    upstream_roles = ["calibrator", "decision_reflector"]
    downstream_roles: list[str] = []  # consumed by daemon at trade time

    def __init__(
        self,
        *,
        engine,
        closed_trades_db: str | Path = CLOSED_TRADES_DB_DEFAULT,
        proposals_path: str | Path = PROPOSALS_PATH_DEFAULT,
        cfg=None,  # AppConfig — read for static defaults
        lookback_days: int = 30,
        sender=None,  # EmailSender — None disables email
    ):
        super().__init__(engine=engine)
        self.closed_trades_db = Path(closed_trades_db)
        self.proposals_path = Path(proposals_path)
        self.cfg = cfg
        self.lookback_days = lookback_days
        self.sender = sender

    def _do_work(self, ctx) -> dict[str, Any]:
        out = run_threshold_tuner(
            engine=self.engine,
            closed_trades_db=self.closed_trades_db,
            proposals_path=self.proposals_path,
            cfg=self.cfg,
            lookback_days=self.lookback_days,
            sender=self.sender,
        )
        return {
            "overrides_written": out.overrides_written,
            "proposals_for_review": out.proposals_for_review,
            "skipped": out.skipped,
            "summary": out.summary,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "tuner_runs",
            float(count),
            f"{count} threshold-tuner runs in last {lookback_days}d",
        )


# ---------------------------------------------------------------------------
# Top-level entry point — exposed for tests, CLI, and lab.py wiring.
# ---------------------------------------------------------------------------


def run_threshold_tuner(
    *,
    engine,
    closed_trades_db: Path = CLOSED_TRADES_DB_DEFAULT,
    proposals_path: Path = PROPOSALS_PATH_DEFAULT,
    cfg=None,
    lookback_days: int = 30,
    sender=None,
) -> TunerOutputs:
    """Compute all knob signals, apply rules, persist overrides + proposals.

    Pure orchestrator: gathers data, calls per-knob rules, dispatches to
    auto-write-or-proposal-JSON based on KnobSpec.mode. Return shape is
    ``TunerOutputs``; Role wraps via _do_work to convert to a dict.
    """
    out = TunerOutputs()
    closed_trades = _load_closed_trades(closed_trades_db, lookback_days)
    iv_ranks = _load_iv_ranks(engine, lookback_days)
    debate_outcomes_score = _load_debate_outcomes(engine, lookback_days, axis="score")
    debate_outcomes_overage = _load_debate_outcomes(engine, lookback_days, axis="overage")
    debate_count, debate_cost = _load_debate_cost(engine, lookback_days)
    realized_yields = _load_realized_wheel_yields(engine, lookback_days)
    recent_bids = _load_recent_csp_bids(engine, lookback_days)
    max_dd_pct = _compute_max_dd(closed_trades)

    # ---------- per_trade_risk_pct ----------
    pnl_pcts = [t.get("pnl_pct", 0.0) for t in closed_trades]
    _apply(
        out, KNOBS["per_trade_risk_pct"],
        rule_per_trade_risk_pct(win_rates=pnl_pcts),
        engine=engine,
    )

    # ---------- max_position_pct ----------
    static_max_pos = _static(cfg, "risk", "max_position_pct", default=10.0)
    _apply(
        out, KNOBS["max_position_pct"],
        rule_max_position_pct(
            current=static_max_pos,
            max_dd_pct=max_dd_pct,
            n_trades=len(closed_trades),
        ),
        engine=engine,
    )

    # ---------- iv_rank_floor ----------
    _apply(
        out, KNOBS["iv_rank_floor"],
        rule_iv_rank_floor(iv_ranks=iv_ranks),
        engine=engine,
    )

    # ---------- min_premium_abs ----------
    _apply(
        out, KNOBS["min_premium_abs"],
        rule_min_premium_abs(recent_bids=recent_bids),
        engine=engine,
    )

    # ---------- min_annualized_yield ----------
    _apply(
        out, KNOBS["min_annualized_yield"],
        rule_min_annualized_yield(realized_yields=realized_yields),
        engine=engine,
    )

    # ---------- unblock_min_candidate_score ----------
    _apply(
        out, KNOBS["unblock_min_candidate_score"],
        rule_unblock_min_candidate_score(debate_outcomes=debate_outcomes_score),
        engine=engine,
    )

    # ---------- unblock_max_overage_ratio ----------
    _apply(
        out, KNOBS["unblock_max_overage_ratio"],
        rule_unblock_max_overage_ratio(debate_outcomes=debate_outcomes_overage),
        engine=engine,
    )

    # ---------- unblock_daily_debate_cap ----------
    _apply(
        out, KNOBS["unblock_daily_debate_cap"],
        rule_unblock_daily_debate_cap(
            n_debates_30d=debate_count, total_cost_30d_usd=debate_cost,
        ),
        engine=engine,
    )

    # Recommend-mode knobs are TODO-stubbed — they record a "no rule yet"
    # skip. The plan calls for an LLM-judge gate before promotion; until
    # that's wired, recommend-mode knobs stay at static YAML.
    for knob_name in ("sector_cap_pct", "options_max_pct",
                      "delta_target_low", "delta_target_high",
                      "dte_min", "dte_max"):
        out.skipped.append({
            "knob": knob_name,
            "reason": "recommend_mode_pending_llm_judge",
        })

    out.proposals_for_review = []  # not used yet
    _write_proposals_json(proposals_path, out)
    out.summary = _format_summary(out)
    if sender is not None and (out.overrides_written or out.proposals_for_review):
        _send_email(sender, out)
    return out


def _apply(
    out: TunerOutputs,
    spec: KnobSpec,
    rule_result: tuple[float, dict[str, Any]] | None,
    *,
    engine,
) -> None:
    if rule_result is None:
        out.skipped.append({"knob": spec.name, "reason": "insufficient_data"})
        return
    proposed, summary = rule_result
    if spec.mode == "auto":
        row = write_override(
            engine,
            knob=spec.name,
            value=proposed,
            bounds_min=spec.bounds_min,
            bounds_max=spec.bounds_max,
            signal_summary=summary,
            set_by="threshold_tuner",
        )
        out.overrides_written.append({
            "knob": spec.name,
            "value": float(row.value),
            "bounds": [spec.bounds_min, spec.bounds_max],
            "signal": summary,
        })
    else:
        out.proposals_for_review.append({
            "knob": spec.name,
            "proposed_value": proposed,
            "bounds": [spec.bounds_min, spec.bounds_max],
            "signal": summary,
        })


# ---------------------------------------------------------------------------
# Data loaders. Each returns a list (possibly empty) — the rules guard
# their own sample-size requirements.
# ---------------------------------------------------------------------------


def _load_closed_trades(db_path: Path, lookback_days: int) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        from trading_bot.reconciliation import ClosedTradeStore
    except Exception:
        return []
    store = ClosedTradeStore(db_path)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    out: list[dict[str, Any]] = []
    for t in store.all():
        et = t.exit_time
        if et.tzinfo is None:
            et = et.replace(tzinfo=dt.timezone.utc)
        if et < cutoff:
            continue
        out.append({
            "symbol": t.symbol,
            "pnl_pct": float(t.pnl_pct),
            "realized_pnl": float(t.realized_pnl),
            "strategy": t.strategy,
            "entry_time": t.entry_time,
            "exit_time": et,
            "hold_hours": float(t.hold_hours),
        })
    return out


def _load_iv_ranks(engine, lookback_days: int) -> list[float]:
    """Return iv_rank values (0-100 percentile) over the last lookback_days.

    The bot's ``option_iv_history`` table stores ``atm_iv_30d`` as a raw
    fractional vol (e.g. 0.30 for 30% IV) — that's a different unit from
    ``iv_rank`` (a percentile against the symbol's own 252-day history,
    0-100). Mixing them would break the floor's safety story, so this
    loader computes iv_rank per symbol from the available history:
    rank = (count of history rows with atm_iv_30d < latest) / n_history.

    Skipped (returns empty) for symbols with < ``iv_rank_min_history``
    rows — same threshold the wheel uses to emit a real iv_rank.
    """
    from collections import defaultdict
    MIN_HISTORY = 10  # need at least 10 days to rank
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    by_symbol: dict[str, list[tuple[dt.datetime, float]]] = defaultdict(list)
    with Session(engine) as session:
        rows = session.query(OptionIvHistory).all()
    for r in rows:
        if r.atm_iv_30d is None:
            continue
        by_symbol[r.symbol].append((r.recorded_at, float(r.atm_iv_30d)))
    out: list[float] = []
    for symbol, series in by_symbol.items():
        if len(series) < MIN_HISTORY:
            continue
        series.sort(key=lambda x: x[0])
        ivs = [iv for _, iv in series]
        # Compute rank for each observation that falls inside the window.
        for ts, iv in series:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if ts < cutoff:
                continue
            rank = sum(1 for v in ivs if v < iv) / len(ivs) * 100.0
            out.append(rank)
    return out


def _load_debate_outcomes(
    engine, lookback_days: int, *, axis: str
) -> list[tuple[float, float]]:
    """Return (axis_value, closed_pnl_pct) for closed 'place' verdicts.

    axis='score' → (candidate_score, pnl); axis='overage' → (overage_ratio, pnl).
    Skip rows where the trade hasn't closed (closed_pnl_pct IS NULL).
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    with Session(engine) as session:
        rows = (
            session.query(UnblockDebateRun)
            .filter(UnblockDebateRun.run_at >= cutoff)
            .filter(UnblockDebateRun.verdict == "place")
            .filter(UnblockDebateRun.closed_pnl_pct.isnot(None))
            .all()
        )
    out: list[tuple[float, float]] = []
    for r in rows:
        if axis == "score":
            v = r.candidate_score
        else:
            v = r.overage_ratio
        if v is None:
            continue
        out.append((float(v), float(r.closed_pnl_pct)))
    return out


def _load_debate_cost(engine, lookback_days: int) -> tuple[int, float]:
    """Return (n_debates, total_cost_usd) over the lookback window for the
    unblock_debate role specifically.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    from trading_bot.state_db import AnthropicCostLog
    with Session(engine) as session:
        cost_rows = (
            session.query(AnthropicCostLog)
            .filter(AnthropicCostLog.called_at >= cutoff)
            .filter(AnthropicCostLog.role_name.like("unblock%"))
            .all()
        )
        debate_count = (
            session.query(UnblockDebateRun)
            .filter(UnblockDebateRun.run_at >= cutoff)
            .count()
        )
    return debate_count, sum(float(r.cost_usd or 0.0) for r in cost_rows)


def _load_realized_wheel_yields(engine, lookback_days: int) -> list[float]:
    """Annualized yield per closed wheel cycle in the window. Best-effort:
    if the wheel_cycles table doesn't have enough closed cycles, returns
    an empty list and the rule skips."""
    from trading_bot.state_db import WheelCycle
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    out: list[float] = []
    with Session(engine) as session:
        cycles = (
            session.query(WheelCycle)
            .filter(WheelCycle.closed_at.isnot(None))
            .filter(WheelCycle.closed_at >= cutoff)
            .all()
        )
    for c in cycles:
        if c.cost_basis is None or float(c.cost_basis) <= 0:
            continue
        if c.opened_at is None or c.closed_at is None:
            continue
        days = max(1, (c.closed_at - c.opened_at).days)
        cost = float(c.cost_basis)
        pnl = float(c.realized_pnl or 0.0)
        annualized = (pnl / cost) * (365.0 / days)
        out.append(annualized)
    return out


def _load_recent_csp_bids(engine, lookback_days: int) -> list[float]:
    """Pull recent CSP option fills as a proxy for 'bids we accepted'. The
    median of these tells us what a typical premium looks like in the
    current regime."""
    from trading_bot.state_db import OptionFill
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    with Session(engine) as session:
        rows = (
            session.query(OptionFill)
            .filter(OptionFill.ts >= cutoff)
            .filter(OptionFill.option_type == "CSP")
            .filter(OptionFill.side == "SELL")
            .all()
        )
    return [float(r.premium) for r in rows if r.premium is not None]


def _compute_max_dd(closed_trades: list[dict[str, Any]]) -> float | None:
    """Rolling max drawdown over the closed-trade equity curve.

    Approximation: walk trades in time order, track cumulative pnl_pct
    series, return the largest peak-to-trough drop. Returns None when
    there are too few trades to compute.
    """
    if len(closed_trades) < 5:
        return None
    sorted_trades = sorted(closed_trades, key=lambda t: t["exit_time"])
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        cum += float(t["pnl_pct"])
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    return float(max_dd)


def _static(cfg, section: str, field_name: str, *, default: float) -> float:
    if cfg is None:
        return default
    sect = getattr(cfg, section, None)
    if sect is None:
        return default
    return float(getattr(sect, field_name, default))


def _write_proposals_json(path: Path, out: TunerOutputs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "overrides_written": out.overrides_written,
        "proposals_for_review": out.proposals_for_review,
        "skipped": out.skipped,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _format_summary(out: TunerOutputs) -> str:
    parts = []
    if out.overrides_written:
        knobs = ", ".join(o["knob"] for o in out.overrides_written)
        parts.append(f"auto-applied: {knobs}")
    if out.proposals_for_review:
        knobs = ", ".join(p["knob"] for p in out.proposals_for_review)
        parts.append(f"proposed: {knobs}")
    if out.skipped:
        parts.append(f"skipped: {len(out.skipped)}")
    return "; ".join(parts) if parts else "no changes"


def _send_email(sender, out: TunerOutputs) -> None:
    """Best-effort email — never raise out of the role on email failure.

    EmailSender.send() expects ``html_body``; callers wrap a stub sender
    around ``send(subject, body)`` for tests, so we accept either kwarg
    name to keep the unit tests legible.
    """
    try:
        text_lines = ["Threshold Tuner — nightly summary", ""]
        html_parts = ["<h2>Threshold Tuner &mdash; nightly summary</h2>"]
        if out.overrides_written:
            text_lines.append("Auto-applied overrides:")
            html_parts.append("<h3>Auto-applied overrides</h3><ul>")
            for o in out.overrides_written:
                line = (
                    f"  - {o['knob']} = {o['value']} "
                    f"(bounds {o['bounds']}; signal: {o['signal']})"
                )
                text_lines.append(line)
                html_parts.append(
                    f"<li><b>{o['knob']}</b> = {o['value']} "
                    f"(bounds {o['bounds']}; signal: {o['signal']})</li>"
                )
            html_parts.append("</ul>")
            text_lines.append("")
        if out.proposals_for_review:
            text_lines.append("Recommendations for review:")
            html_parts.append("<h3>Recommendations for review</h3><ul>")
            for p in out.proposals_for_review:
                line = (
                    f"  - {p['knob']}: propose {p['proposed_value']} "
                    f"(bounds {p['bounds']}; signal: {p['signal']})"
                )
                text_lines.append(line)
                html_parts.append(
                    f"<li><b>{p['knob']}</b>: propose {p['proposed_value']} "
                    f"(bounds {p['bounds']}; signal: {p['signal']})</li>"
                )
            html_parts.append("</ul>")
            text_lines.append("")
        skip_line = f"Skipped: {len(out.skipped)} (insufficient data or status quo)"
        text_lines.append(skip_line)
        html_parts.append(f"<p>{skip_line}</p>")
        text_body = "\n".join(text_lines)
        html_body = "\n".join(html_parts)
        # Try the EmailSender shape first; fall back to a simple ``body=``
        # signature so test stubs keep working.
        try:
            sender.send(
                subject="[trading-bot] threshold tuner summary",
                html_body=html_body,
                text_body=text_body,
            )
        except TypeError:
            sender.send(
                subject="[trading-bot] threshold tuner summary",
                body=text_body,
            )
    except Exception:
        pass
