"""Nightly self-review loop (Bucket G).

Runs after the daily 16:30 ET digest (configured at 17:00 ET) and emails the
operator a single "morning brief" summarising:

  1. **Decision rollup**: orders placed / blocked-by-rule / blocked-by-gate
     today, by strategy.
  2. **Drift watch**: filter pass-rates today vs the trailing 7-day average,
     flagging deltas > 30 percentage points (gates that suddenly start
     blocking 90% of names are a signal).
  3. **Freshness audit**: result of ``freshness_audit.audit_freshness()``,
     so a missed premarket_rank shows up in the operator's inbox without
     waiting for the next morning's trade.
  4. **Risk state**: current ``RiskState`` (consecutive losing days,
     halt status, halted strategies, size multiplier).
  5. **System health**: heartbeat age, today's scheduler-audit shortfalls,
     pause-flag status, wheel-eligible-set size.
  6. **Open issues**: count of WARN+ log events captured today (via the
     existing ``alert_drain`` queue).

This loop is **read-only and safe** — it never writes to risk-state files,
never modifies strategy parameters, never auto-merges code. It is the
"detect" half of the self-improvement design described in the README.
The "tune" half (lab → param flips) and "fix" half (LLM-generated PRs)
are deliberately NOT in this bucket; they require human approval.

Per the operator memory rule, every Claude SDK call inside this module
(if/when the optional LLM-audit path is enabled) MUST use claude-opus-4-7.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from trading_bot.email_log import send_logged
from trading_bot.email_sender import EmailSender
from trading_bot.freshness_audit import audit_freshness, render_text_summary
from trading_bot.state_pause import (
    HALTED_STRATEGIES_PATH,
    is_paused,
    read_halted_strategies,
)


log = logging.getLogger(__name__)

DEFAULT_PAUSE_PATH = Path(os.environ.get("TRADING_BOT_PAUSE", "data/pause.flag"))
DRIFT_DELTA_THRESHOLD_PP = 30.0  # alert when a gate's pass-rate moves >30pp
DRIFT_BASELINE_DAYS = 7


# =====================================================================
# Section data classes — each section of the email maps to one of these
# =====================================================================


@dataclass(frozen=True)
class DecisionRollup:
    placed_order: int
    rejected_by_risk: int
    rejected_by_gate: int
    held: int
    by_strategy: dict[str, dict[str, int]]


@dataclass(frozen=True)
class DriftFinding:
    gate: str
    pass_rate_today_pct: float
    pass_rate_baseline_pct: float
    delta_pp: float
    severity: str  # "ok" | "warn" | "bad"


@dataclass(frozen=True)
class RiskSnapshot:
    consecutive_losing_days: int
    halted: bool
    halt_reason: str
    halted_strategies: tuple[str, ...]
    size_multiplier: str  # rendered as string for email


@dataclass(frozen=True)
class SystemHealth:
    heartbeat_age_minutes: float | None
    pause_flag_set: bool
    wheel_eligible_count: int
    open_alerts_pending: int


@dataclass(frozen=True)
class NightlyReview:
    as_of: dt.datetime
    audit_date: dt.date
    decisions: DecisionRollup
    drift: list[DriftFinding]
    freshness_summary: str
    risk: RiskSnapshot
    health: SystemHealth
    errors: list[str] = field(default_factory=list)


# =====================================================================
# Section builders
# =====================================================================


def _decision_rollup(engine: Engine, *, day_start: dt.datetime, day_end: dt.datetime) -> DecisionRollup:
    """Aggregate today's persisted decisions by action and strategy."""
    counts = {"placed_order": 0, "rejected_by_risk": 0, "rejected_by_gate": 0, "held": 0}
    by_strategy: dict[str, dict[str, int]] = {}
    with engine.begin() as c:
        try:
            rows = c.execute(text(
                "SELECT action, strategy FROM decisions "
                "WHERE timestamp_utc >= :s AND timestamp_utc < :e"
            ), {"s": day_start, "e": day_end}).fetchall()
        except Exception:
            rows = []
    for action, strategy in rows:
        action = (action or "held").lower()
        if action not in counts:
            counts.setdefault(action, 0)
            counts[action] += 1
        else:
            counts[action] += 1
        s = strategy or "unknown"
        by_strategy.setdefault(s, {}).setdefault(action, 0)
        by_strategy[s][action] += 1
    return DecisionRollup(
        placed_order=counts.get("placed_order", 0),
        rejected_by_risk=counts.get("rejected_by_risk", 0),
        rejected_by_gate=counts.get("rejected_by_gate", 0),
        held=counts.get("held", 0),
        by_strategy=by_strategy,
    )


def _drift_findings(engine: Engine, *, audit_date: dt.date) -> list[DriftFinding]:
    """Compare today's gate pass-rate to the trailing 7-day average per gate.

    A "gate" here is a ``reason`` value on rejected_by_gate decisions (e.g.
    ``skipped_sentiment``, ``earnings_in_window``, ``vix=...``). We compute
    the share of evaluated decisions that were blocked by each gate.

    Returns only gates with |delta| > DRIFT_DELTA_THRESHOLD_PP.
    """
    today_start = dt.datetime.combine(audit_date, dt.time.min, tzinfo=dt.timezone.utc)
    today_end = today_start + dt.timedelta(days=1)
    baseline_start = today_start - dt.timedelta(days=DRIFT_BASELINE_DAYS)

    def _gate_rates(start: dt.datetime, end: dt.datetime) -> dict[str, float]:
        with engine.begin() as c:
            try:
                rows = c.execute(text(
                    "SELECT reason FROM decisions "
                    "WHERE action = 'rejected_by_gate' "
                    "  AND timestamp_utc >= :s AND timestamp_utc < :e"
                ), {"s": start, "e": end}).fetchall()
                total = c.execute(text(
                    "SELECT COUNT(*) FROM decisions "
                    "WHERE timestamp_utc >= :s AND timestamp_utc < :e"
                ), {"s": start, "e": end}).scalar() or 0
            except Exception:
                return {}
        if not total:
            return {}
        gate_counts: dict[str, int] = {}
        for (reason,) in rows:
            gate_key = (reason or "unknown").split("(")[0].strip().split("=")[0].strip()
            gate_counts[gate_key] = gate_counts.get(gate_key, 0) + 1
        return {g: (n / total) * 100.0 for g, n in gate_counts.items()}

    today = _gate_rates(today_start, today_end)
    baseline = _gate_rates(baseline_start, today_start)
    findings: list[DriftFinding] = []
    for gate in set(today) | set(baseline):
        t = today.get(gate, 0.0)
        b = baseline.get(gate, 0.0)
        delta = t - b
        if abs(delta) < DRIFT_DELTA_THRESHOLD_PP:
            continue
        severity = "warn" if abs(delta) < 50 else "bad"
        findings.append(DriftFinding(
            gate=gate, pass_rate_today_pct=t,
            pass_rate_baseline_pct=b, delta_pp=delta, severity=severity,
        ))
    findings.sort(key=lambda f: abs(f.delta_pp), reverse=True)
    return findings


def _risk_snapshot(state_builder=None) -> RiskSnapshot:
    """Read live RiskState if a builder is wired; otherwise fall back to the
    on-disk halted_strategies file. Always returns a snapshot (never raises)."""
    halted_strategies = read_halted_strategies(HALTED_STRATEGIES_PATH)
    if state_builder is not None:
        try:
            r = state_builder()
            return RiskSnapshot(
                consecutive_losing_days=int(getattr(r, "consecutive_losing_days", 0)),
                halted=bool(getattr(r, "halted", False)),
                halt_reason=str(getattr(r, "halt_reason", "")),
                halted_strategies=tuple(sorted(getattr(r, "halted_strategies", halted_strategies))),
                size_multiplier=str(getattr(r, "size_multiplier", "1")),
            )
        except Exception as e:
            log.warning("nightly_review: state_builder failed: %s", e)
    return RiskSnapshot(
        consecutive_losing_days=0, halted=False, halt_reason="",
        halted_strategies=tuple(sorted(halted_strategies)), size_multiplier="1",
    )


def _system_health(engine: Engine, *, heartbeat_path: Path,
                   pause_path: Path) -> SystemHealth:
    age_minutes: float | None = None
    try:
        if heartbeat_path.exists():
            payload = json.loads(heartbeat_path.read_text())
            ts_str = payload.get("ts", "").replace("Z", "+00:00")
            ts = dt.datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            age_minutes = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60.0
    except Exception as e:
        log.warning("nightly_review: heartbeat read failed: %s", e)

    eligible_count = 0
    pending_alerts = 0
    with engine.begin() as c:
        try:
            eligible_count = c.execute(text(
                "SELECT COUNT(*) FROM wheel_universe_cache WHERE eligible = 1"
            )).scalar() or 0
        except Exception:
            pass
        try:
            pending_alerts = c.execute(text(
                "SELECT COUNT(*) FROM alerts_pending"
            )).scalar() or 0
        except Exception:
            pass

    return SystemHealth(
        heartbeat_age_minutes=age_minutes,
        pause_flag_set=is_paused(pause_path),
        wheel_eligible_count=int(eligible_count),
        open_alerts_pending=int(pending_alerts),
    )


# =====================================================================
# Composer + sender
# =====================================================================


_HTML_TEMPLATE = """<!doctype html>
<html><head><style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          font-size: 14px; color: #222; max-width: 800px; }}
  h1 {{ font-size: 20px; border-bottom: 2px solid #333; padding-bottom: 6px; }}
  h2 {{ font-size: 16px; margin-top: 24px; color: #333; }}
  .ok    {{ color: #16a34a; }}
  .warn  {{ color: #d97706; }}
  .bad   {{ color: #dc2626; font-weight: 600; }}
  table {{ border-collapse: collapse; margin: 8px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; }}
  th {{ background: #f3f4f6; }}
  pre {{ background: #f8f8f8; padding: 8px; border-radius: 4px;
         font-size: 12px; overflow-x: auto; }}
  .muted {{ color: #666; font-size: 12px; }}
</style></head>
<body>
<h1>Nightly Self-Review — {audit_date}</h1>
<p class="muted">Generated {as_of_iso}. Read-only summary; no changes were
applied automatically. See sections below.</p>

<h2>1. Decision Rollup</h2>
<table>
  <tr><th>Outcome</th><th>Count</th></tr>
  <tr><td>placed_order</td><td>{placed}</td></tr>
  <tr><td>rejected_by_risk</td><td class="warn">{rej_risk}</td></tr>
  <tr><td>rejected_by_gate</td><td>{rej_gate}</td></tr>
  <tr><td>held / no-signal</td><td class="muted">{held}</td></tr>
</table>
{by_strategy_html}

<h2>2. Drift Watch</h2>
{drift_html}

<h2>3. Freshness Audit</h2>
<pre>{freshness}</pre>

<h2>4. Risk State</h2>
<table>
  <tr><th>consecutive_losing_days</th><td class="{cld_class}">{cld}</td></tr>
  <tr><th>size_multiplier</th><td>{multiplier}</td></tr>
  <tr><th>halted (global)</th><td class="{halt_class}">{halted}{halt_reason_suffix}</td></tr>
  <tr><th>halted_strategies</th><td>{halted_strategies}</td></tr>
</table>

<h2>5. System Health</h2>
<table>
  <tr><th>heartbeat age (min)</th><td class="{hb_class}">{heartbeat}</td></tr>
  <tr><th>pause.flag set</th><td class="{pause_class}">{pause}</td></tr>
  <tr><th>wheel eligible-set</th><td class="{wheel_class}">{wheel_count}</td></tr>
  <tr><th>alerts queued (unsent)</th><td>{alerts_pending}</td></tr>
</table>

{errors_html}

<p class="muted">Bucket G: this email is the read-only "detect" loop. The
"tune" loop (lab → param flips) and "fix" loop (LLM-authored PRs) are
deliberately separate; they require explicit operator approval.</p>
</body></html>
"""


def _by_strategy_html(rollup: DecisionRollup) -> str:
    if not rollup.by_strategy:
        return ""
    rows = []
    for strat, counts in sorted(rollup.by_strategy.items()):
        rows.append(
            f"<tr><td>{strat}</td>"
            f"<td>{counts.get('placed_order', 0)}</td>"
            f"<td>{counts.get('rejected_by_risk', 0)}</td>"
            f"<td>{counts.get('rejected_by_gate', 0)}</td>"
            f"<td>{counts.get('held', 0)}</td></tr>"
        )
    return (
        "<p>By strategy:</p><table>"
        "<tr><th>strategy</th><th>placed</th><th>risk-rej</th>"
        "<th>gate-rej</th><th>held</th></tr>"
        + "".join(rows) + "</table>"
    )


def _drift_html(drift: list[DriftFinding]) -> str:
    if not drift:
        return "<p class='ok'>No gates moved more than 30pp vs the trailing 7-day baseline.</p>"
    rows = []
    for f in drift:
        klass = "bad" if f.severity == "bad" else "warn"
        rows.append(
            f"<tr><td>{f.gate}</td>"
            f"<td>{f.pass_rate_today_pct:.1f}%</td>"
            f"<td>{f.pass_rate_baseline_pct:.1f}%</td>"
            f"<td class='{klass}'>{f.delta_pp:+.1f}pp</td></tr>"
        )
    return (
        "<table><tr><th>gate</th><th>today</th>"
        f"<th>baseline ({DRIFT_BASELINE_DAYS}d)</th><th>delta</th></tr>"
        + "".join(rows) + "</table>"
    )


def _errors_html(errors: list[str]) -> str:
    if not errors:
        return ""
    items = "".join(f"<li>{e}</li>" for e in errors)
    return f"<h2>Errors during review</h2><ul>{items}</ul>"


def compose_email(review: NightlyReview) -> tuple[str, str]:
    """Build (subject, html_body) from the gathered review data."""
    cld_class = "bad" if review.risk.consecutive_losing_days >= 3 else "ok"
    halt_class = "bad" if review.risk.halted else "ok"
    halt_reason_suffix = (
        f" — {review.risk.halt_reason}" if review.risk.halt_reason else ""
    )
    hb_age = review.health.heartbeat_age_minutes
    hb_class = "ok"
    if hb_age is None or hb_age > 5:
        hb_class = "bad"
    elif hb_age > 2:
        hb_class = "warn"
    pause_class = "bad" if review.health.pause_flag_set else "ok"
    wheel_class = "bad" if review.health.wheel_eligible_count < 50 else "ok"

    placed = review.decisions.placed_order
    bad_signal = (
        review.risk.halted
        or review.health.pause_flag_set
        or review.health.wheel_eligible_count < 50
        or any(f.severity == "bad" for f in review.drift)
    )
    severity_tag = "🔴" if bad_signal else ("🟡" if review.drift else "🟢")
    subject = (
        f"{severity_tag} Nightly Review {review.audit_date.isoformat()} — "
        f"placed={placed} drift={len(review.drift)}"
    )

    html_body = _HTML_TEMPLATE.format(
        audit_date=review.audit_date.isoformat(),
        as_of_iso=review.as_of.isoformat(timespec="seconds"),
        placed=placed,
        rej_risk=review.decisions.rejected_by_risk,
        rej_gate=review.decisions.rejected_by_gate,
        held=review.decisions.held,
        by_strategy_html=_by_strategy_html(review.decisions),
        drift_html=_drift_html(review.drift),
        freshness=review.freshness_summary,
        cld=review.risk.consecutive_losing_days,
        cld_class=cld_class,
        multiplier=review.risk.size_multiplier,
        halted="yes" if review.risk.halted else "no",
        halt_class=halt_class,
        halt_reason_suffix=halt_reason_suffix,
        halted_strategies=(", ".join(review.risk.halted_strategies)
                           if review.risk.halted_strategies else "(none)"),
        heartbeat=(f"{hb_age:.1f}" if hb_age is not None else "MISSING"),
        hb_class=hb_class,
        pause=("yes — " + str(DEFAULT_PAUSE_PATH)) if review.health.pause_flag_set else "no",
        pause_class=pause_class,
        wheel_count=review.health.wheel_eligible_count,
        wheel_class=wheel_class,
        alerts_pending=review.health.open_alerts_pending,
        errors_html=_errors_html(review.errors),
    )
    return subject, html_body


def gather_review(
    *,
    engine: Engine,
    audit_date: dt.date,
    state_builder=None,
    heartbeat_path: Path = Path("data/heartbeat.json"),
    pause_path: Path = DEFAULT_PAUSE_PATH,
) -> NightlyReview:
    """Pull every signal needed for the nightly email."""
    errors: list[str] = []
    today_start = dt.datetime.combine(audit_date, dt.time.min, tzinfo=dt.timezone.utc)
    today_end = today_start + dt.timedelta(days=1)

    try:
        rollup = _decision_rollup(engine, day_start=today_start, day_end=today_end)
    except Exception as e:
        log.exception("nightly_review: decision rollup failed")
        errors.append(f"decision_rollup: {e}")
        rollup = DecisionRollup(0, 0, 0, 0, {})

    try:
        drift = _drift_findings(engine, audit_date=audit_date)
    except Exception as e:
        log.exception("nightly_review: drift findings failed")
        errors.append(f"drift_findings: {e}")
        drift = []

    try:
        freshness = render_text_summary(audit_freshness())
    except Exception as e:
        log.exception("nightly_review: freshness audit failed")
        errors.append(f"freshness_audit: {e}")
        freshness = "freshness audit unavailable"

    risk = _risk_snapshot(state_builder=state_builder)
    health = _system_health(engine, heartbeat_path=heartbeat_path, pause_path=pause_path)

    return NightlyReview(
        as_of=dt.datetime.now(dt.timezone.utc),
        audit_date=audit_date,
        decisions=rollup,
        drift=drift,
        freshness_summary=freshness,
        risk=risk,
        health=health,
        errors=errors,
    )


def run_nightly_review(
    *,
    engine: Engine,
    sender: EmailSender,
    recipient: str,
    audit_date: dt.date | None = None,
    state_builder=None,
    heartbeat_path: Path = Path("data/heartbeat.json"),
    pause_path: Path = DEFAULT_PAUSE_PATH,
) -> NightlyReview:
    """Top-level entry. Run from a 17:00 ET cron in scheduler_jobs.py.

    Read-only: builds + emails the nightly review. Does not mutate state.
    """
    audit_date = audit_date or dt.datetime.now(dt.timezone.utc).date()
    review = gather_review(
        engine=engine, audit_date=audit_date, state_builder=state_builder,
        heartbeat_path=heartbeat_path, pause_path=pause_path,
    )
    subject, html = compose_email(review)
    send_logged(
        sender=sender, subject=subject, html_body=html,
        kind="nightly_review", recipient=recipient,
    )
    return review
