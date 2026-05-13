"""Strategy version writer + active-version reader.

Plan v4 §3 + §13. Every strategy lives as a sequence of immutable
``strategy_version`` rows. "Current version" = MAX(strategy_ver) for a
given strategy_id, subject to expiry.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Optional

from trading_bot.registry.schema import ensure_registry_tables

RESEARCH_ONLY = "research_only"
ACTIVE_TRADING_STATUSES = frozenset({"tiny_paper", "scaled_paper", "live"})
EXIT_ONLY_STATUSES = frozenset({"reduce_only", "observe_only"})
NON_ACTIVE_STATUSES = frozenset({
    RESEARCH_ONLY, "shadow", "halted",
})


class VersionNotFound(Exception):
    """Raised when no strategy_version row exists for the given id."""


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    strategy_ver: int
    code_hash: str
    config_hash: str
    thesis_id: str
    hypothesis_id: str
    validation_artifact_id: Optional[str]
    lane: str
    status: str
    expiry_date: Optional[dt.date]
    owner: str
    created_ts: dt.datetime

    def is_active_for_trading(self, now: Optional[dt.datetime] = None) -> bool:
        """Returns True iff status is in ACTIVE_TRADING_STATUSES AND not
        past expiry. Plan §13: 90-day expiry unless re-validated."""
        if self.status not in ACTIVE_TRADING_STATUSES:
            return False
        if self.expiry_date is None:
            return True
        now = now or dt.datetime.now(dt.timezone.utc)
        today = now.date()
        return today <= self.expiry_date


def register_version(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_ver: int,
    code_hash: str,
    config_hash: str,
    thesis_id: str,
    hypothesis_id: str,
    lane: str,
    owner: str,
    status: str = RESEARCH_ONLY,
    validation_artifact_id: Optional[str] = None,
    expiry_date: Optional[dt.date] = None,
    now: Optional[dt.datetime] = None,
) -> StrategyVersion:
    ensure_registry_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    # research_only versions don't need a validation_artifact_id;
    # promoting away from research_only is gated by registry.promotion.
    if status in ACTIVE_TRADING_STATUSES and validation_artifact_id is None:
        raise ValueError(
            f"cannot register {strategy_id} v{strategy_ver} at status="
            f"{status} without a validation_artifact_id"
        )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO strategy_version (
            strategy_id, strategy_ver, code_hash, config_hash,
            thesis_id, hypothesis_id, validation_artifact_id,
            lane, status, expiry_date, owner, created_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            strategy_id, strategy_ver, code_hash, config_hash,
            thesis_id, hypothesis_id, validation_artifact_id,
            lane, status,
            expiry_date.isoformat() if expiry_date else None,
            owner, now.isoformat(),
        ),
    )
    return StrategyVersion(
        strategy_id=strategy_id, strategy_ver=strategy_ver,
        code_hash=code_hash, config_hash=config_hash,
        thesis_id=thesis_id, hypothesis_id=hypothesis_id,
        validation_artifact_id=validation_artifact_id,
        lane=lane, status=status, expiry_date=expiry_date,
        owner=owner, created_ts=now,
    )


def get_active_version(
    conn: sqlite3.Connection, strategy_id: str,
) -> StrategyVersion:
    """Return the latest version row for ``strategy_id``."""
    ensure_registry_tables(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT strategy_id, strategy_ver, code_hash, config_hash,
               thesis_id, hypothesis_id, validation_artifact_id,
               lane, status, expiry_date, owner, created_ts
        FROM strategy_version
        WHERE strategy_id = ?
        ORDER BY strategy_ver DESC
        LIMIT 1
        """,
        (strategy_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise VersionNotFound(strategy_id)
    return StrategyVersion(
        strategy_id=row[0], strategy_ver=row[1], code_hash=row[2],
        config_hash=row[3], thesis_id=row[4], hypothesis_id=row[5],
        validation_artifact_id=row[6], lane=row[7], status=row[8],
        expiry_date=dt.date.fromisoformat(row[9]) if row[9] else None,
        owner=row[10],
        created_ts=dt.datetime.fromisoformat(row[11]),
    )


def list_versions(
    conn: sqlite3.Connection, strategy_id: str,
) -> list[StrategyVersion]:
    ensure_registry_tables(conn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT strategy_id, strategy_ver, code_hash, config_hash,
               thesis_id, hypothesis_id, validation_artifact_id,
               lane, status, expiry_date, owner, created_ts
        FROM strategy_version
        WHERE strategy_id = ?
        ORDER BY strategy_ver ASC
        """,
        (strategy_id,),
    )
    out: list[StrategyVersion] = []
    for r in cur.fetchall():
        out.append(StrategyVersion(
            strategy_id=r[0], strategy_ver=r[1], code_hash=r[2],
            config_hash=r[3], thesis_id=r[4], hypothesis_id=r[5],
            validation_artifact_id=r[6], lane=r[7], status=r[8],
            expiry_date=dt.date.fromisoformat(r[9]) if r[9] else None,
            owner=r[10],
            created_ts=dt.datetime.fromisoformat(r[11]),
        ))
    return out


__all__ = [
    "ACTIVE_TRADING_STATUSES",
    "EXIT_ONLY_STATUSES",
    "NON_ACTIVE_STATUSES",
    "RESEARCH_ONLY",
    "StrategyVersion",
    "VersionNotFound",
    "get_active_version",
    "list_versions",
    "register_version",
]
