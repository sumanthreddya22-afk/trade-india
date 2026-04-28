"""Promotion atomicity + 10% improvement gate tests."""
from __future__ import annotations

import json

import pytest

from trading_bot.promotion import (
    MIN_FITNESS_DELTA,
    PromotionCandidate,
    promote_atomically,
    should_promote,
)


@pytest.fixture
def active_path(tmp_path):
    p = tmp_path / "paper_active.json"
    p.write_text(
        json.dumps(
            {
                "version": "test-v1",
                "active_template": "momentum",
                "params": {"rsi_lower": 55.0, "rsi_upper": 70.0},
                "fitness_at_promotion": 1.0,
            }
        )
    )
    return p


def test_should_promote_when_delta_exceeds_gate(active_path):
    candidate = PromotionCandidate(
        template="momentum",
        params={"rsi_lower": 56.0},
        fitness=1.5,  # 50% improvement
        alpha_vs_spy_x=1.8,
        sortino=1.4,
        max_dd_pct=15.0,
    )
    ok, info = should_promote(active_path, candidate)
    assert ok is True
    assert info["delta_pct"] > MIN_FITNESS_DELTA * 100


def test_should_not_promote_when_delta_below_gate(active_path):
    candidate = PromotionCandidate(
        template="momentum",
        params={"rsi_lower": 56.0},
        fitness=1.05,  # 5% improvement only
        alpha_vs_spy_x=1.8,
        sortino=1.4,
        max_dd_pct=15.0,
    )
    ok, info = should_promote(active_path, candidate)
    assert ok is False
    assert "delta" in info["reason"].lower()


def test_should_not_promote_when_gate_check_fails(active_path):
    """Even with big fitness gain, promotion gate (alpha/sortino/dd thresholds) must pass."""
    candidate = PromotionCandidate(
        template="momentum",
        params={"rsi_lower": 56.0},
        fitness=99.0,
        alpha_vs_spy_x=1.4,  # below MIN_ALPHA_VS_SPY (1.5)
        sortino=2.0,
        max_dd_pct=10.0,
    )
    ok, info = should_promote(active_path, candidate)
    assert ok is False
    assert "gate" in info["reason"].lower()


def test_should_promote_when_no_active_fitness(tmp_path):
    """First promotion: no incumbent fitness → promote if gates pass."""
    p = tmp_path / "active.json"
    p.write_text(
        json.dumps(
            {
                "version": "bootstrap",
                "active_template": "momentum",
                "params": {},
                "fitness_at_promotion": None,
            }
        )
    )
    candidate = PromotionCandidate(
        template="momentum",
        params={"rsi_lower": 56.0},
        fitness=1.5,
        alpha_vs_spy_x=1.6,
        sortino=1.1,
        max_dd_pct=18.0,
    )
    ok, _ = should_promote(p, candidate)
    assert ok is True


def test_promote_atomically_rewrites_file(active_path):
    candidate = PromotionCandidate(
        template="momentum_v4",
        params={"rsi_lower": 58.0, "rsi_upper": 72.0},
        fitness=1.7,
        alpha_vs_spy_x=1.8,
        sortino=1.4,
        max_dd_pct=15.0,
    )
    promote_atomically(active_path, candidate)
    written = json.loads(active_path.read_text())
    assert written["active_template"] == "momentum_v4"
    assert written["params"]["rsi_lower"] == 58.0
    assert written["fitness_at_promotion"] == 1.7
    # No leftover .tmp file
    assert not (active_path.parent / (active_path.name + ".tmp")).exists()


def test_promote_preserves_other_keys(active_path):
    """Promotion must not destroy unrelated config sections (cadence, risk_caps, etc.)."""
    cfg = json.loads(active_path.read_text())
    cfg["risk_caps"] = {"max_position_pct": 10}
    cfg["cadence"] = {"heartbeat_seconds": 60}
    active_path.write_text(json.dumps(cfg))

    candidate = PromotionCandidate(
        template="momentum_v5",
        params={"rsi_lower": 60.0},
        fitness=1.5,
        alpha_vs_spy_x=1.6,
        sortino=1.1,
        max_dd_pct=18.0,
    )
    promote_atomically(active_path, candidate)
    written = json.loads(active_path.read_text())
    assert written["risk_caps"] == {"max_position_pct": 10}
    assert written["cadence"] == {"heartbeat_seconds": 60}
