"""Promotion gate.

Plan v4 §14 P1: "Strategy without a Tier-1 artifact cannot enter paper.
Without a Tier-2, cannot go to scaled paper. Without a Tier-3, cannot go
live."

For Phase 4 the gate consumes the validation_artifact rows + the
validation_policy lock + (optionally) a promotion_packet for Tier-3.
Phase 5+ wires the multi-persona panel; the schema field
``risk_review_id`` is already in promotion_packet so we can plug it in
without schema migration.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from trading_bot.ledger.hash_chain import compute_this_hash, last_hash
from trading_bot.registry.schema import ensure_registry_tables
from trading_bot.registry.strategies import (
    ACTIVE_TRADING_STATUSES, RESEARCH_ONLY,
)
from trading_bot.registry.validation_artifacts import (
    TIER_LIVE, TIER_PAPER, TIER_RESEARCH, find_latest_pass,
)

# Status -> required tier mapping per Plan §13 (which artifact you need
# to BE at this status).
_REQUIRED_TIER = {
    "shadow": TIER_RESEARCH,
    "tiny_paper": TIER_PAPER,
    "scaled_paper": TIER_PAPER,
    "live": TIER_LIVE,
}

# Tier-3 (live) requires a human-signed promotion_packet.
_REQUIRES_HUMAN_SIGNOFF = frozenset({"live"})


@dataclass(frozen=True)
class PromotionDecision:
    allowed: bool
    reason: str
    target_status: str
    tier_required: Optional[str] = None
    artifact_id: Optional[str] = None
    human_signoff_required: bool = False
    promotion_packet_id: Optional[str] = None


def gate(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_ver: int,
    target_status: str,
    validation_policy_lock: Mapping,
    promotion_packet_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> PromotionDecision:
    """Single-entry promotion check.

    The target_status must be one of {shadow, tiny_paper, scaled_paper,
    live}. Statuses like research_only or observe_only / reduce_only are
    set by other paths (registry.register_version for initial creation;
    risk.lane_caps.demote_on_breach for autonomous demotion).
    """
    ensure_registry_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    tier_required = _REQUIRED_TIER.get(target_status)
    if tier_required is None:
        return PromotionDecision(
            allowed=False,
            reason=f"promotion target {target_status!r} is not a "
                   f"promotable status",
            target_status=target_status,
        )

    artifact = find_latest_pass(
        conn, strategy_id=strategy_id, tier=tier_required,
    )
    if artifact is None:
        return PromotionDecision(
            allowed=False,
            reason=f"no passing Tier-{tier_required} artifact for "
                   f"{strategy_id}",
            target_status=target_status,
            tier_required=tier_required,
        )

    if artifact["strategy_ver"] != strategy_ver:
        return PromotionDecision(
            allowed=False,
            reason=f"artifact ver={artifact['strategy_ver']} does not "
                   f"match requested ver={strategy_ver}",
            target_status=target_status,
            tier_required=tier_required,
            artifact_id=artifact["artifact_id"],
        )

    requires_signoff = target_status in _REQUIRES_HUMAN_SIGNOFF
    if requires_signoff:
        if promotion_packet_id is None:
            return PromotionDecision(
                allowed=False,
                reason=f"target {target_status!r} requires a "
                       f"human-signed promotion_packet",
                target_status=target_status,
                tier_required=tier_required,
                artifact_id=artifact["artifact_id"],
                human_signoff_required=True,
            )
        cur = conn.cursor()
        cur.execute(
            "SELECT operator_signed, expiry_date, validation_artifact_id, "
            "strategy_id, strategy_ver "
            "FROM promotion_packet WHERE packet_id = ?",
            (promotion_packet_id,),
        )
        row = cur.fetchone()
        if row is None:
            return PromotionDecision(
                allowed=False,
                reason=f"promotion_packet {promotion_packet_id!r} not found",
                target_status=target_status,
                tier_required=tier_required,
                artifact_id=artifact["artifact_id"],
                human_signoff_required=True,
            )
        op_signed, expiry, packet_artifact_id, pkt_sid, pkt_ver = row
        if not op_signed:
            return PromotionDecision(
                allowed=False,
                reason="promotion_packet is not operator-signed",
                target_status=target_status,
                tier_required=tier_required,
                artifact_id=artifact["artifact_id"],
                human_signoff_required=True,
                promotion_packet_id=promotion_packet_id,
            )
        if pkt_sid != strategy_id or pkt_ver != strategy_ver:
            return PromotionDecision(
                allowed=False,
                reason="promotion_packet does not reference this strategy version",
                target_status=target_status,
                tier_required=tier_required,
                artifact_id=artifact["artifact_id"],
                human_signoff_required=True,
                promotion_packet_id=promotion_packet_id,
            )
        if packet_artifact_id != artifact["artifact_id"]:
            return PromotionDecision(
                allowed=False,
                reason="promotion_packet references a different artifact",
                target_status=target_status,
                tier_required=tier_required,
                artifact_id=artifact["artifact_id"],
                human_signoff_required=True,
                promotion_packet_id=promotion_packet_id,
            )
        if dt.date.fromisoformat(expiry) < now.date():
            return PromotionDecision(
                allowed=False,
                reason="promotion_packet is expired",
                target_status=target_status,
                tier_required=tier_required,
                artifact_id=artifact["artifact_id"],
                human_signoff_required=True,
                promotion_packet_id=promotion_packet_id,
            )

    return PromotionDecision(
        allowed=True,
        reason="ok",
        target_status=target_status,
        tier_required=tier_required,
        artifact_id=artifact["artifact_id"],
        human_signoff_required=requires_signoff,
        promotion_packet_id=promotion_packet_id,
    )


def _canonical(payload: Mapping) -> bytes:
    return json.dumps(dict(payload), sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")


def compute_packet_id(*, payload: Mapping) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def record_promotion_packet(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_ver: int,
    target_tier: str,
    code_hash: str,
    config_hash: str,
    validation_artifact_id: str,
    paper_scorecard_id: Optional[str] = None,
    risk_review_id: Optional[str] = None,
    known_failure_modes: Optional[Iterable[str]] = None,
    expiry_date: Optional[dt.date] = None,
    operator_signed: bool = False,
    now: Optional[dt.datetime] = None,
) -> str:
    """Insert one promotion_packet row. Returns packet_id."""
    ensure_registry_tables(conn)
    now = now or dt.datetime.now(dt.timezone.utc)
    expiry_date = expiry_date or (now.date() + dt.timedelta(days=90))
    kfm_json = json.dumps(
        list(known_failure_modes or []),
        sort_keys=True, separators=(",", ":"),
    )
    payload = {
        "strategy_id": strategy_id, "strategy_ver": strategy_ver,
        "target_tier": target_tier, "code_hash": code_hash,
        "config_hash": config_hash,
        "validation_artifact_id": validation_artifact_id,
        "paper_scorecard_id": paper_scorecard_id,
        "risk_review_id": risk_review_id,
        "known_failure_modes": list(known_failure_modes or []),
        "expiry_date": expiry_date.isoformat(),
        "operator_signed": 1 if operator_signed else 0,
        "created_ts": now.isoformat(),
    }
    packet_id = compute_packet_id(payload=payload)
    prev = last_hash(conn, "promotion_packet")
    row_for_hash = {
        **payload, "packet_id": packet_id,
        "known_failure_modes_json": kfm_json,
    }
    row_for_hash.pop("known_failure_modes", None)
    this_hash = compute_this_hash(prev, row_for_hash)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO promotion_packet (
            packet_id, strategy_id, strategy_ver, target_tier,
            code_hash, config_hash, validation_artifact_id,
            paper_scorecard_id, risk_review_id, known_failure_modes_json,
            expiry_date, operator_signed, created_ts,
            prev_hash, this_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            packet_id, strategy_id, strategy_ver, target_tier,
            code_hash, config_hash, validation_artifact_id,
            paper_scorecard_id, risk_review_id, kfm_json,
            expiry_date.isoformat(), 1 if operator_signed else 0,
            now.isoformat(), prev, this_hash,
        ),
    )
    return packet_id


__all__ = [
    "PromotionDecision",
    "compute_packet_id",
    "gate",
    "record_promotion_packet",
]
