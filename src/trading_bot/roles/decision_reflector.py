"""Decision Reflector — Tier 5 lab role.

For each closed trade in the lookback window that does not yet have a
:class:`~trading_bot.state_db.DecisionLesson`, this role:

  1. Joins the trade's ``entry_order_id`` to its originating ``decisions``
     row (skips trades whose decision was logged before this loop existed).
  2. Asks Claude (via ``complete_structured`` with a forced tool schema)
     for a 2-4 sentence post-mortem keyed to (symbol, strategy, regime,
     reason, realized P&L, hold hours).
  3. Writes the lesson to ``decision_lessons`` so future architect /
     reasoning prompts can be primed with hindsight via
     :func:`trading_bot.decision_lessons.recent_lessons_text`.

Pattern is the markdown-memory loop from TauricResearch/TradingAgents'
``agents/utils/memory.py``, ported to a SQLite-backed table joined on
``entry_order_id``. No vector store, no embeddings — flat recency + a
small-N prompt injection is plenty given the trade volume.

The role is read-mostly: it writes only to ``decision_lessons``. It must
NOT be wired into the read-only Bucket G nightly review (which advertises
itself as side-effect-free) — schedule it in its own job.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trading_bot.anthropic_client import (
    AnthropicClient,
    AnthropicCredsMissingError,
    BudgetExceededError,
    default_architect_model,
)
from trading_bot.decision_lessons import append_lesson, has_lesson
from trading_bot.reconciliation import ClosedTradeStore
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import Decisions, DecisionLesson, RoleRun


CLOSED_TRADES_DB_DEFAULT = Path("data/closed_trades.db")
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MAX_PER_RUN = 10  # cap LLM spend per run; backlog catches up over days


class _LessonOutput(BaseModel):
    lesson: str = Field(
        min_length=20,
        max_length=600,
        description=(
            "2-4 sentences. Specific, actionable insight grounded in this "
            "trade's symbol, strategy, regime, entry reason, and realised "
            "outcome. Avoid platitudes ('always size correctly'); prefer "
            "concrete pattern names ('over-stayed past 1.5R into reversal')."
        ),
    )
    tags: list[
        Literal[
            "good_entry", "bad_entry",
            "good_exit", "bad_exit", "overstayed",
            "stop_hit", "tp_hit", "time_exit",
            "trend_aligned", "trend_against",
            "regime_mismatch", "iv_crush", "earnings_window",
            "size_too_small", "size_too_large",
            "noise", "high_alpha",
        ]
    ] = Field(
        default_factory=list,
        max_length=4,
        description="Up to 4 categorical tags drawn from the controlled vocabulary.",
    )


_LESSON_TOOL_SCHEMA = _LessonOutput.model_json_schema()


_SYSTEM_PROMPT = """You are the Decision Reflector for an autonomous \
trading system. For each closed trade you receive, write a SHORT post-mortem.

INPUT: a single trade record with the original decision context (symbol, \
strategy, regime, entry reason, confidence) and its realised outcome \
(P&L %, hold hours). You will be told whether the trade was profitable.

TASK: produce a 2-4 sentence lesson explaining the most likely *reason* \
the trade resolved as it did. Tie it to the inputs we actually had at \
decision time — not to information that arrived later. Then pick up to \
4 tags from the controlled vocabulary that describe the pattern.

WHAT TO AVOID:
- Generic advice ("manage risk", "follow your plan").
- Restating the inputs without analysis.
- Fabricating market context that wasn't in the input.

WHAT TO PREFER:
- Naming the specific pattern: "stop hit on intraday reversal after \
  failed breakout"; "TP held through earnings window — lucky, not \
  repeatable"; "regime flipped from trending_up to sideways mid-hold".
- Linking to the strategy's stated edge: "momentum entry but RSI was \
  already 78 — late-cycle signal".
"""


class DecisionReflectorRole(BaseRole):
    name = "decision_reflector"
    tier = 5
    process = "lab"
    job_description = (
        "Write 2-4 sentence post-mortems for recently closed trades, store "
        "them in decision_lessons. Capped per run to bound LLM spend."
    )
    sla_seconds = 5 * 60
    upstream_roles = ["reconciler"]
    downstream_roles = ["strategy_architect", "strategy_coach"]

    def __init__(
        self,
        *,
        engine,
        closed_trades_db: str | Path = CLOSED_TRADES_DB_DEFAULT,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        max_per_run: int = DEFAULT_MAX_PER_RUN,
    ):
        super().__init__(engine=engine)
        self.closed_trades_db = Path(closed_trades_db)
        self.lookback_days = lookback_days
        self.max_per_run = max_per_run

    def _do_work(self, ctx):
        try:
            client = AnthropicClient(
                role_name=self.name, model=default_architect_model(), engine=self.engine
            )
        except AnthropicCredsMissingError:
            return {"skipped": True, "reason": "no_anthropic_creds"}

        if not self.closed_trades_db.exists():
            return {"skipped": True, "reason": "no_closed_trades_db"}

        candidates = self._unreflected_trades()
        if not candidates:
            return {"reflected": 0, "reason": "nothing_to_reflect"}

        candidates = candidates[: self.max_per_run]
        wrote = 0
        skipped_text_only = 0
        errors: list[str] = []

        for pair in candidates:
            trade = pair["trade"]
            decision = pair["decision"]
            try:
                resp = client.complete_structured(
                    system=_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": _format_user_msg(trade, decision)}],
                    tool_name="record_lesson",
                    tool_description="Submit a 2-4 sentence post-mortem with up to 4 tags.",
                    tool_schema=_LESSON_TOOL_SCHEMA,
                    max_tokens=400,
                )
            except BudgetExceededError as e:
                errors.append(f"budget_halt:{e}")
                break
            except Exception as e:
                errors.append(f"{decision['decision_id']}:{type(e).__name__}:{e}")
                continue

            if resp.used_structured and resp.data:
                lesson_text = str(resp.data.get("lesson", "")).strip()
                tags = list(resp.data.get("tags", []))
            elif resp.text.strip():
                # Free-text fallback: write the prose as-is, no tags.
                lesson_text = resp.text.strip()[:600]
                tags = []
                skipped_text_only += 1
            else:
                continue

            if not lesson_text:
                continue

            inserted = append_lesson(
                self.engine,
                decision_id=decision["decision_id"],
                entry_order_id=decision["entry_order_id"],
                symbol=trade.symbol,
                strategy=trade.strategy or decision["strategy"] or "unknown",
                regime=decision.get("regime", "") or trade.regime,
                pnl_pct=float(trade.pnl_pct),
                hold_hours=float(trade.hold_hours),
                lesson=lesson_text,
                tags=tags,
            )
            if inserted:
                wrote += 1

        return {
            "reflected": wrote,
            "candidates": len(candidates),
            "skipped_text_only": skipped_text_only,
            "errors": errors,
        }

    def _unreflected_trades(self) -> list[dict]:
        """Return [{trade, decision}] for closed trades within lookback that
        don't yet have a lesson AND have a matching ``decisions`` row."""
        store = ClosedTradeStore(self.closed_trades_db)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.lookback_days)
        # ClosedTradeStore doesn't expose a between() — read all and filter.
        # Trade volume is small (paper account), so the full scan is fine.
        with Session(store._engine) as s:  # type: ignore[attr-defined]
            from trading_bot.reconciliation import _ClosedTradeRow  # local import
            rows = (
                s.query(_ClosedTradeRow)
                .filter(_ClosedTradeRow.exit_time >= cutoff)
                .order_by(_ClosedTradeRow.exit_time.desc())
                .all()
            )
            trades = [
                _row_to_trade(r) for r in rows
            ]

        out: list[dict] = []
        with Session(self.engine) as s:
            for t in trades:
                d = (
                    s.query(Decisions)
                    .filter(Decisions.entry_order_id == t.entry_order_id)
                    .order_by(Decisions.timestamp_utc.desc())
                    .first()
                )
                if d is None:
                    continue
                if has_lesson(self.engine, decision_id=d.decision_id):
                    continue
                out.append(
                    {
                        "trade": t,
                        "decision": {
                            "decision_id": d.decision_id,
                            "entry_order_id": d.entry_order_id,
                            "symbol": d.symbol,
                            "strategy": d.strategy,
                            "regime": d.regime,
                            "reason": d.reason,
                            "confidence": d.confidence,
                            "expected_edge_bps": d.expected_edge_bps,
                            "timestamp_utc": d.timestamp_utc.isoformat()
                            if d.timestamp_utc
                            else "",
                        },
                    }
                )
        return out

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            runs = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
            lessons = (
                session.query(DecisionLesson)
                .filter(DecisionLesson.created_at >= cutoff)
                .count()
            )
        rate = float(lessons) / max(runs, 1)
        return (
            "lessons_per_run",
            rate,
            f"{lessons} lessons across {runs} reflector runs in last {lookback_days}d",
        )


def _row_to_trade(r):
    """Convert a _ClosedTradeRow into the public ClosedTrade dataclass —
    the store's append() does this implicitly but we need the read path."""
    from decimal import Decimal

    from trading_bot.reconciliation import ClosedTrade

    return ClosedTrade(
        symbol=r.symbol,
        side=r.side,
        qty=Decimal(str(r.qty)),
        entry_price=Decimal(str(r.entry_price)),
        exit_price=Decimal(str(r.exit_price)),
        realized_pnl=Decimal(str(r.realized_pnl)),
        pnl_pct=float(r.pnl_pct),
        strategy=r.strategy,
        regime=r.regime,
        entry_time=r.entry_time,
        exit_time=r.exit_time,
        hold_hours=float(r.hold_hours),
        entry_order_id=r.entry_order_id,
        notes=r.notes or "",
    )


def _format_user_msg(trade, decision: dict) -> str:
    """Compose a concise, structured prompt the model can reflect on."""
    profitable = "PROFITABLE" if trade.pnl_pct > 0 else "LOSING"
    parts = [
        f"Trade outcome: {profitable} ({trade.pnl_pct:+.2f}% over {trade.hold_hours:.1f}h)",
        "",
        "Decision context (what we knew at entry):",
        f"  symbol:           {trade.symbol}",
        f"  strategy:         {decision.get('strategy') or trade.strategy}",
        f"  regime_at_entry:  {decision.get('regime') or trade.regime}",
        f"  entry_reason:     {decision.get('reason', '')}",
        f"  confidence:       {decision.get('confidence')}",
        f"  expected_edge_bps:{decision.get('expected_edge_bps')}",
        f"  decision_at:      {decision.get('timestamp_utc', '')}",
        "",
        "Realised:",
        f"  entry_price:  {trade.entry_price}",
        f"  exit_price:   {trade.exit_price}",
        f"  realized_pnl: {trade.realized_pnl}",
        f"  notes:        {trade.notes}",
    ]
    return "\n".join(parts)


def run_decision_reflection(
    engine,
    *,
    closed_trades_db: str | Path = CLOSED_TRADES_DB_DEFAULT,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_per_run: int = DEFAULT_MAX_PER_RUN,
) -> dict:
    """Stand-alone entry point: run one reflection pass and return the
    role's ``outputs`` dict. Useful for ad-hoc operator-triggered runs and
    for the future scheduler hook."""
    role = DecisionReflectorRole(
        engine=engine,
        closed_trades_db=closed_trades_db,
        lookback_days=lookback_days,
        max_per_run=max_per_run,
    )
    return role.safe_run(ctx={}).outputs
