"""Append-only store for Decision audit records.

Wraps the `decisions` table. Only insert and read methods are exposed —
no update or delete API. This is intentional: the decision log is the
forensic spine of the system and must be tamper-evident.

Companion to ``trading_bot.trade_journal.TradeJournal`` (which logs only
the trades that actually got placed). The decision store logs *everything*
the bot considered, including rejections and skips.
"""
from __future__ import annotations

import json
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.orchestrator import Decision, decision_to_dict
from trading_bot.state_db import Decisions, get_engine


@dataclass(frozen=True)
class DecisionRow:
    """In-memory view of a `decisions` row. JSON columns are returned raw —
    callers parse if they need the structured sub-objects."""

    decision_id: str
    timestamp_utc: datetime
    symbol: str
    action: str
    reason: str
    strategy: str
    regime: str
    asset_class: str
    confidence: float | None
    expected_edge_bps: float | None
    risk_after_json: str
    compliance_json: str
    data_quality_json: str
    execution_constraints_json: str
    alerts_json: str
    audit_json: str
    entry_order_id: str
    stop_loss_order_id: str


def _new_decision_id() -> str:
    """Time-prefixed random id so chronological ordering is naturally readable."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"d_{ts}_{secrets.token_hex(6)}"


class DecisionStore:
    """Append-only access layer for the `decisions` table."""

    def __init__(self, db_path: str | Path) -> None:
        self._engine = get_engine(db_path)

    def append(
        self,
        decision: Decision,
        *,
        strategy: str,
        regime: str,
        asset_class: str,
    ) -> str:
        """Insert one decision. Returns the decision_id."""
        d = decision_to_dict(decision)
        decision_id = _new_decision_id()
        # Prefer the audit object's timestamp when present so tests/fixtures
        # can pin a deterministic time; fall back to "now" for live runs.
        ts_iso = decision.audit.timestamp_utc
        if ts_iso:
            try:
                ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        row = Decisions(
            decision_id=decision_id,
            timestamp_utc=ts,
            symbol=decision.symbol,
            action=decision.action,
            reason=decision.reason,
            strategy=strategy,
            regime=regime,
            asset_class=asset_class,
            confidence=decision.confidence,
            expected_edge_bps=decision.expected_edge_bps,
            risk_after_json=json.dumps(d["risk_after"]),
            compliance_json=json.dumps(d["compliance"]),
            data_quality_json=json.dumps(d["data_quality"]),
            execution_constraints_json=json.dumps(d["execution_constraints"]),
            alerts_json=json.dumps(d["alerts"]),
            audit_json=json.dumps(d["audit"]),
            entry_order_id=decision.entry_order_id,
            stop_loss_order_id=decision.stop_loss_order_id,
        )
        with Session(self._engine) as s:
            s.add(row)
            s.commit()
        return decision_id

    def recent(self, *, limit: int = 100) -> list[DecisionRow]:
        with Session(self._engine) as s:
            rows = (
                s.execute(
                    select(Decisions)
                    .order_by(Decisions.timestamp_utc.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._to_row(r) for r in rows]

    def between(self, start: datetime, end: datetime) -> list[DecisionRow]:
        with Session(self._engine) as s:
            rows = (
                s.execute(
                    select(Decisions)
                    .where(
                        Decisions.timestamp_utc >= start,
                        Decisions.timestamp_utc <= end,
                    )
                    .order_by(Decisions.timestamp_utc)
                )
                .scalars()
                .all()
            )
            return [self._to_row(r) for r in rows]

    def action_counts(self, *, since: datetime | None = None) -> dict[str, int]:
        """Count decisions grouped by action. Useful for the daily digest."""
        with Session(self._engine) as s:
            stmt = select(Decisions.action)
            if since is not None:
                stmt = stmt.where(Decisions.timestamp_utc >= since)
            actions = list(s.execute(stmt).scalars())
        return dict(Counter(actions))

    def top_rejection_reasons(
        self, *, since: datetime | None = None, limit: int = 5,
    ) -> list[tuple[str, int]]:
        """Top reason strings for non-placed decisions (skips/rejects)."""
        with Session(self._engine) as s:
            stmt = select(Decisions.action, Decisions.reason)
            if since is not None:
                stmt = stmt.where(Decisions.timestamp_utc >= since)
            rows = list(s.execute(stmt))
        reasons = [
            r[1] for r in rows
            if r[0] not in ("placed_order",) and r[1]
        ]
        counts = Counter(reasons).most_common(limit)
        return counts

    @staticmethod
    def _to_row(r: Decisions) -> DecisionRow:
        return DecisionRow(
            decision_id=r.decision_id,
            timestamp_utc=r.timestamp_utc,
            symbol=r.symbol,
            action=r.action,
            reason=r.reason,
            strategy=r.strategy,
            regime=r.regime,
            asset_class=r.asset_class,
            confidence=r.confidence,
            expected_edge_bps=r.expected_edge_bps,
            risk_after_json=r.risk_after_json,
            compliance_json=r.compliance_json,
            data_quality_json=r.data_quality_json,
            execution_constraints_json=r.execution_constraints_json,
            alerts_json=r.alerts_json,
            audit_json=r.audit_json,
            entry_order_id=r.entry_order_id,
            stop_loss_order_id=r.stop_loss_order_id,
        )
