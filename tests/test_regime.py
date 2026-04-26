import numpy as np
import pandas as pd

from trading_bot.regime import Regime, detect_regime_from_bars


def _bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": closes,
         "open": closes,
         "high": [c * 1.005 for c in closes],
         "low": [c * 0.995 for c in closes],
         "volume": [1_000_000] * len(closes)},
        index=pd.date_range("2025-01-01", periods=len(closes), freq="D", tz="UTC"),
    )


def test_regime_trending_up():
    """Steady uptrend → TRENDING_UP."""
    closes = [100 + i * 0.5 for i in range(220)]
    reading = detect_regime_from_bars(_bars(closes))
    assert reading.regime == Regime.TRENDING_UP


def test_regime_trending_down():
    """Steady downtrend → TRENDING_DOWN."""
    closes = [200 - i * 0.5 for i in range(220)]
    reading = detect_regime_from_bars(_bars(closes))
    assert reading.regime == Regime.TRENDING_DOWN


def test_regime_risk_off_on_high_vol():
    """High realized vol → RISK_OFF."""
    rng = np.random.default_rng(42)
    base = 100.0
    closes = [base]
    # Massive 5% daily moves for 25 days drives annualized vol > 30%
    for _ in range(220):
        closes.append(closes[-1] * (1 + rng.choice([-0.05, 0.05])))
    reading = detect_regime_from_bars(_bars(closes))
    assert reading.regime == Regime.RISK_OFF


def test_regime_risk_off_on_drawdown():
    """Sharp drawdown → RISK_OFF."""
    closes = [100 + i * 0.2 for i in range(200)]  # uptrend
    closes += [closes[-1] * 0.85] * 20  # 15% drop, held flat 20 days
    reading = detect_regime_from_bars(_bars(closes))
    assert reading.regime == Regime.RISK_OFF


def test_regime_short_history_returns_sideways():
    reading = detect_regime_from_bars(_bars([100.0] * 10))
    assert reading.regime == Regime.SIDEWAYS
    assert reading.confidence == "low"


def test_regime_vix_above_28_forces_risk_off():
    """Phase 0c: VIX > 28 overrides bars-only logic, even on a calm uptrend."""
    closes = [100 + i * 0.5 for i in range(220)]  # would be TRENDING_UP
    reading = detect_regime_from_bars(_bars(closes), vix=30.0)
    assert reading.regime == Regime.RISK_OFF
    assert "VIX 30.0" in reading.notes
    assert reading.vix == 30.0


def test_regime_vix_above_22_caps_to_sideways():
    """Phase 0c: VIX in (22, 28] prevents TRENDING_UP classification."""
    closes = [100 + i * 0.5 for i in range(220)]  # would be TRENDING_UP
    reading = detect_regime_from_bars(_bars(closes), vix=24.0)
    assert reading.regime == Regime.SIDEWAYS
    assert "VIX 24.0" in reading.notes
    assert reading.vix == 24.0


def test_regime_vix_low_does_not_override():
    """Calm VIX leaves bars-only result intact."""
    closes = [100 + i * 0.5 for i in range(220)]
    reading = detect_regime_from_bars(_bars(closes), vix=15.0)
    assert reading.regime == Regime.TRENDING_UP


def test_regime_vol_threshold_lowered_to_22_triggers_risk_off():
    """Phase 0b: vol around 25% should trigger risk_off at threshold=22 but
    not at the legacy 30."""
    rng = np.random.default_rng(7)
    base = 100.0
    closes = [base]
    for _ in range(220):
        # ~1.5% daily moves → annualised vol roughly 24%
        closes.append(closes[-1] * (1 + rng.choice([-0.015, 0.015])))
    bars = _bars(closes)
    at_22 = detect_regime_from_bars(bars, vol_threshold_pct=22.0)
    at_30 = detect_regime_from_bars(bars, vol_threshold_pct=30.0)
    assert at_22.regime == Regime.RISK_OFF
    assert at_30.regime != Regime.RISK_OFF
