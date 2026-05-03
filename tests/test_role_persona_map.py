"""Tests for role → persona operator map (system dashboard wiring)."""
from __future__ import annotations

import pytest

from trading_bot.shared.role_persona_map import (
    ROLE_OPERATORS,
    operators_for_role,
    operators_payload,
)


# ---------------------------------------------------------------------------
# operators_for_role: registry resolution
# ---------------------------------------------------------------------------


def test_unknown_role_returns_empty_list():
    assert operators_for_role("never-registered") == []


def test_intel_scan_resolves_three_stocks_personas():
    ops = operators_for_role("intel_scan")
    names = [op.full_name for op in ops]
    # Order: skeptic, analyst, judge (per ROLE_OPERATORS convention)
    assert names == ["Jonas Vance", "Eleanor Park", "Margaret Holloway"]
    # All belong to the stocks pipeline
    for op in ops:
        assert op.pipeline == "stocks"


def test_crypto_scan_resolves_three_crypto_personas():
    ops = operators_for_role("crypto_scan")
    names = [op.full_name for op in ops]
    assert "Sasha Volkov" in names
    assert "Lena Park" in names
    assert "Diane Pereira" in names
    for op in ops:
        assert op.pipeline == "crypto"


def test_wheel_scan_resolves_four_options_personas():
    ops = operators_for_role("wheel_scan")
    debate_roles = [op.debate_role for op in ops]
    assert debate_roles == [
        "wheel_aggressive", "wheel_conservative",
        "wheel_neutral", "wheel_judge",
    ]
    for op in ops:
        assert op.pipeline == "options"


def test_portfolio_watch_resolves_hold_personas():
    """Stocks portfolio watcher fires hold debates."""
    ops = operators_for_role("portfolio_watch")
    names = {op.full_name for op in ops}
    # Hold team: Daniel Reyes, Theodore Granger, Olivia Brennan, Margaret Holloway
    assert "Daniel Reyes" in names
    assert "Theodore Granger" in names
    assert "Olivia Brennan" in names
    assert "Margaret Holloway" in names


def test_data_only_jobs_have_no_operators():
    """Reconcilers, log rotation, etc. have no LLM personas."""
    for role in ("reconciler", "log_rotation", "iv_capture", "verify_stops",
                 "schedule_audit", "alert_drain", "heartbeat"):
        assert operators_for_role(role) == [], f"role {role} should have no operators"


def test_nightly_review_pulls_lesson_analysts_across_pipelines():
    ops = operators_for_role("nightly_review")
    names = {op.full_name for op in ops}
    assert "Helena Wu" in names         # stocks lesson analyst
    assert "Theo Marchetti" in names    # crypto lesson analyst
    assert "Mira Bhatt" in names        # options lesson analyst


# ---------------------------------------------------------------------------
# operators_payload: template-friendly form
# ---------------------------------------------------------------------------


def test_payload_returns_list_of_dicts():
    payload = operators_payload("crypto_scan")
    assert all(isinstance(p, dict) for p in payload)
    sasha = next(p for p in payload if p["name"] == "Sasha Volkov")
    assert sasha["pipeline"] == "crypto"
    assert sasha["debate_role"] == "scout_skeptic"
    assert sasha["title"]


def test_payload_unknown_role_returns_empty():
    assert operators_payload("does-not-exist") == []


# ---------------------------------------------------------------------------
# Registry coverage — every persona id referenced must resolve
# ---------------------------------------------------------------------------


def test_every_referenced_persona_id_resolves():
    """Catches typos in ROLE_OPERATORS — every id must exist as a real PERSONA."""
    referenced = {pid for ids in ROLE_OPERATORS.values() for pid in ids}
    for pid in referenced:
        # The lookup uses a list-comprehension that drops unresolved ids
        # silently. Verify by checking the registry directly.
        from trading_bot.shared.role_persona_map import _registry
        reg = _registry()
        assert pid in reg, f"ROLE_OPERATORS references unknown persona id: {pid}"
