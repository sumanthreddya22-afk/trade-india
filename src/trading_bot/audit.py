"""Audit-object resolver.

Deterministic, no-side-effects helpers for building the per-decision
``AuditObject`` (W1.3 of the PDF-parity plan). The orchestrator (and any
other component that creates a Decision) is expected to call ``build_audit``
with the data it actually used — this module does not query the database or
the network on the hot path.

The audit object is the forensic spine: given a ``decision_id`` from the
``decisions`` table, you should be able to fully reconstruct what code,
what model, what data, what regime, and what policy version produced it.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.orchestrator import AuditObject


def hash_string(s: str) -> str:
    """16-char hex hash. Stable, content-derived. Deliberately not a full SHA;
    used for compact identifiers in audit fields, not cryptographic integrity."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _git_sha_from_env() -> str:
    """Best-effort: prefer GIT_SHA env var (set by deploy), else read .git/HEAD.
    Never raises; returns empty string when nothing usable is found."""
    sha = os.environ.get("GIT_SHA", "").strip()
    if sha:
        return sha
    try:
        head = Path(".git/HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_path = Path(".git") / ref
            if ref_path.exists():
                return ref_path.read_text().strip()
        else:
            return head
    except Exception:
        pass
    return ""


def compute_policy_version(
    *,
    config_path: Path,
    git_sha: str | None = None,
) -> str:
    """Build a policy_version identifier.

    Format: ``<git_sha[:8]>_<config_hash[:8]>``. Both halves change when their
    underlying source changes; identical inputs always produce identical
    output (deterministic). Missing config file → empty config hash, never
    raises.
    """
    sha = (git_sha or _git_sha_from_env() or "")[:8]
    try:
        contents = config_path.read_text()
        config_hash = hash_string(contents)[:8]
    except Exception:
        config_hash = "00000000"
    return f"{sha}_{config_hash}"


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with trailing 'Z' (matches PDF examples)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_audit(
    *,
    strategy: str,
    regime: str,
    policy_version: str,
    model_versions: dict[str, str] | None = None,
    prompt_versions: dict[str, str] | None = None,
    data_snapshot_ids: tuple[str, ...] = (),
    risk_state_id: str = "",
    timestamp_utc: str | None = None,
    strategy_source: str | None = None,
) -> AuditObject:
    """Assemble an AuditObject for a Decision.

    ``strategy`` is the registered strategy name (e.g., ``"momentum"``).
    ``strategy_source`` is the raw strategy module text — when supplied, its
    hash is appended to ``strategy_version`` so prompt/code edits show up in
    the audit even without a Lab promotion.  When omitted, only the strategy
    name + a stable hash of that name is used.
    """
    src = strategy_source if strategy_source is not None else strategy
    strategy_version = f"{strategy}:{hash_string(src)[:8]}"
    return AuditObject(
        policy_version=policy_version,
        strategy_version=strategy_version,
        model_versions=dict(model_versions or {}),
        prompt_versions=dict(prompt_versions or {}),
        data_snapshot_ids=tuple(data_snapshot_ids),
        regime=regime,
        risk_state_id=risk_state_id,
        timestamp_utc=timestamp_utc or _utcnow_iso(),
    )
