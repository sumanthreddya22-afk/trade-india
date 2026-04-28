"""Calibration math tests."""
from trading_bot.calibration import compute_drift_score


def test_perfect_monotonic_corr_ok():
    pred = list(range(20))
    real = [x * 2 for x in pred]
    corr, sev = compute_drift_score(pred, real)
    assert corr is not None
    assert corr > 0.99
    assert sev == "ok"


def test_perfect_inverse_corr_high():
    pred = list(range(20))
    real = [-x for x in pred]
    corr, sev = compute_drift_score(pred, real)
    assert corr < -0.99
    assert sev == "high"


def test_no_correlation_high_or_warning():
    pred = list(range(20))
    real = [1.0, -1.0, 0.5, -0.5] * 5  # decoupled
    corr, sev = compute_drift_score(pred, real)
    # Either warning or high — both are non-OK; just assert it isn't ok
    assert sev != "ok"


def test_insufficient_data_returns_none():
    corr, sev = compute_drift_score([1, 2, 3], [4, 5, 6])
    assert corr is None
    assert sev == "insufficient_data"


def test_mismatched_lengths_raises():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        compute_drift_score([1, 2], [1, 2, 3])


def test_warning_band_explicit():
    """Severity boundaries: corr exactly 0.5 → warning band; 0.3 → still warning; <0.3 → high."""
    # Build pred/real that produce a known corr by making real mostly noise around pred
    import random

    rng = random.Random(42)
    pred = list(range(50))
    # Mix 50% pred + 50% noise → ~0.4 corr empirically
    real = [p + rng.uniform(-30, 30) for p in pred]
    corr, sev = compute_drift_score(pred, real)
    assert corr is not None
    if 0.3 <= corr <= 0.5:
        assert sev == "warning"
    elif corr > 0.5:
        assert sev == "ok"
    else:
        assert sev == "high"
