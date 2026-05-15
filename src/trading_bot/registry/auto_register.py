"""Auto-register a new strategy version on a passed paper-validation
(v4 Phase C).

Voids the moment ``live_capital.lock.live_capital_enabled == True`` —
the fast-track path is paper-only.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from trading_bot.registry.strategies import register_version
from trading_bot.risk import DEFAULT_POLICY_DIR

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoRegisterResult:
    registered: bool
    reason: str
    new_strategy_ver: Optional[int]


def _live_capital_disabled(policy_dir: Path = DEFAULT_POLICY_DIR) -> bool:
    p = policy_dir / "live_capital.lock"
    if not p.exists():
        return True
    try:
        payload = json.loads(p.read_text())
    except json.JSONDecodeError:
        return True
    return not bool(payload.get("live_capital_enabled", False))


def _fast_track_enabled(policy_dir: Path = DEFAULT_POLICY_DIR) -> bool:
    p = policy_dir / "paper_fast_track_v1.lock"
    if not p.exists():
        return False
    try:
        payload = json.loads(p.read_text())
    except json.JSONDecodeError:
        return False
    return bool(payload.get("enabled"))


def auto_register_v_n_plus_1(
    conn: sqlite3.Connection,
    *,
    strategy_id_base: str,
    candidate_params: dict,
    candidate_id: str,
    code_hash: str,
    config_hash: str,
    thesis_id: str,
    hypothesis_id: str,
    lane: str,
    owner: str = "mutation_engine",
    policy_dir: Path = DEFAULT_POLICY_DIR,
    target_status: str = "tiny_paper",
    now: Optional[dt.datetime] = None,
) -> AutoRegisterResult:
    """Insert a new strategy_version row at ``target_status`` for the
    mutated candidate. Only fires when:
      * live_capital is disabled, AND
      * paper_fast_track_v1.lock is enabled.
    Otherwise the candidate stays as a research_only record and the
    operator must hand-promote it.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if not _live_capital_disabled(policy_dir):
        return AutoRegisterResult(
            registered=False,
            reason="live_capital is enabled — auto-register voided",
            new_strategy_ver=None,
        )
    if not _fast_track_enabled(policy_dir):
        return AutoRegisterResult(
            registered=False,
            reason="paper_fast_track_v1.lock is not enabled",
            new_strategy_ver=None,
        )

    # Determine the next version number for this family.
    cur = conn.execute(
        "SELECT MAX(strategy_ver) FROM strategy_version WHERE strategy_id=?",
        (strategy_id_base,),
    )
    row = cur.fetchone()
    base_ver = int(row[0]) if row and row[0] is not None else 0
    new_ver = base_ver + 1

    # The strategies module requires a validation_artifact_id for any
    # status in ACTIVE_TRADING_STATUSES. The mutation-engine path passes
    # the candidate's paper_validation_event ledger_seq as a stand-in
    # artifact (the event is hash-chained + auditable).
    artifact_id = candidate_id  # paper_validation_event id used as artifact
    register_version(
        conn,
        strategy_id=strategy_id_base,
        strategy_ver=new_ver,
        code_hash=code_hash,
        config_hash=config_hash,
        thesis_id=thesis_id,
        hypothesis_id=hypothesis_id,
        lane=lane,
        owner=owner,
        status=target_status,
        validation_artifact_id=artifact_id,
        now=now,
    )
    conn.commit()
    return AutoRegisterResult(
        registered=True,
        reason=(
            f"auto-registered {strategy_id_base} v={new_ver} at "
            f"{target_status} via candidate={candidate_id}"
        ),
        new_strategy_ver=new_ver,
    )


__all__ = ["AutoRegisterResult", "auto_register_v_n_plus_1"]
