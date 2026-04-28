"""Fitness scoring tests."""
from trading_bot.fitness import FitnessScore, compute_fitness, promotion_gate_check


def test_compute_fitness_normal():
    score = compute_fitness(alpha_vs_spy_x=1.8, sortino=1.4, max_dd_pct=15.0)
    assert isinstance(score, FitnessScore)
    assert score.fitness_score > 0


def test_dd_penalty_kicks_in_above_20():
    s_under = compute_fitness(
        alpha_vs_spy_x=2.0, sortino=1.5, max_dd_pct=10.0
    ).fitness_score
    s_over = compute_fitness(
        alpha_vs_spy_x=2.0, sortino=1.5, max_dd_pct=30.0
    ).fitness_score
    assert s_over < s_under


def test_promotion_gate_pass():
    score = compute_fitness(alpha_vs_spy_x=1.6, sortino=1.1, max_dd_pct=18.0)
    assert promotion_gate_check(score) is True


def test_promotion_gate_fail_low_alpha():
    score = compute_fitness(alpha_vs_spy_x=1.4, sortino=2.0, max_dd_pct=10.0)
    assert promotion_gate_check(score) is False


def test_promotion_gate_fail_low_sortino():
    score = compute_fitness(alpha_vs_spy_x=2.0, sortino=0.5, max_dd_pct=10.0)
    assert promotion_gate_check(score) is False


def test_promotion_gate_fail_high_dd():
    score = compute_fitness(alpha_vs_spy_x=2.0, sortino=2.0, max_dd_pct=25.0)
    assert promotion_gate_check(score) is False


def test_promotion_gate_at_thresholds_passes():
    """Boundary case — exactly at min thresholds should pass."""
    score = compute_fitness(alpha_vs_spy_x=1.5, sortino=1.0, max_dd_pct=20.0)
    assert promotion_gate_check(score) is True
