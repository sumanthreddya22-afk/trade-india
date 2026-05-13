"""Phase 5 — ablation monotone-degradation + parameter-plateau coverage."""
from __future__ import annotations

from trading_bot.research import is_monotone_degradation, plateau_coverage


# ---------------------------------------------------------------------------
# Ablation
# ---------------------------------------------------------------------------


def test_ablation_pass_when_monotone() -> None:
    series = [
        ("full", 1.50), ("no-vol-adj", 1.20),
        ("no-momentum", 0.80), ("baseline", 0.10),
    ]
    r = is_monotone_degradation(series)
    assert r.monotone
    assert r.violations == ()


def test_ablation_fail_when_stripping_features_improves() -> None:
    series = [
        ("full", 1.20), ("no-vol-adj", 1.50),    # removing improves!
    ]
    r = is_monotone_degradation(series)
    assert not r.monotone
    assert len(r.violations) == 1


def test_ablation_within_tolerance_ok() -> None:
    series = [("full", 1.00), ("stripped", 1.000001)]
    assert is_monotone_degradation(series, tolerance=1e-3).monotone


def test_ablation_single_point_trivially_passes() -> None:
    r = is_monotone_degradation([("only", 1.0)])
    assert r.monotone


# ---------------------------------------------------------------------------
# Plateau
# ---------------------------------------------------------------------------


def test_plateau_full_width_when_metric_flat() -> None:
    r = plateau_coverage({1.0: 0.9, 2.0: 0.9, 3.0: 0.9}, tolerance=0.05)
    assert r.plateau_fraction == 1.0


def test_plateau_narrow_when_single_spike() -> None:
    r = plateau_coverage({
        1.0: 0.0, 2.0: 0.0, 3.0: 1.0, 4.0: 0.0, 5.0: 0.0,
    }, tolerance=0.05)
    # Only one point at the maximum.
    assert r.plateau_fraction == 0.0


def test_plateau_meets_25_percent_threshold() -> None:
    # Five evenly-spaced parameters: 1, 2, 3, 4, 5.
    # Two contiguous values at 0.9 (within 0.05 of max 0.92).
    r = plateau_coverage({
        1.0: 0.40, 2.0: 0.91, 3.0: 0.92, 4.0: 0.50, 5.0: 0.40,
    }, tolerance=0.05)
    # plateau width = 3 - 2 = 1; total width = 5 - 1 = 4 → 25%.
    assert r.plateau_fraction == 0.25


def test_plateau_handles_two_points() -> None:
    r = plateau_coverage({1.0: 0.9, 2.0: 0.5}, tolerance=0.1)
    # max=0.9; only one point within tolerance → 0% plateau.
    assert r.plateau_fraction == 0.0
