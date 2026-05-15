"""Black-Scholes pricing + Greeks (pure-Python, no scipy dependency).

Used by:
  * ``ingest.yfinance_adapter.find_contract_by_delta`` — strike selection
    for the wheel strategy (sell 0.30-delta puts).
  * ``strategies.spy_wheel_v1`` — delta filter for exit signals.
  * Backtest-lite for the wheel — approximate option pricing when
    historical chains aren't available.

Conventions:
  * S = spot, K = strike, T = years to expiry, r = risk-free rate,
    sigma = annualised implied volatility (as a decimal, 0.20 = 20%).
  * Returns floats. Returns 0.0 (not NaN) on degenerate inputs
    (T <= 0 or sigma <= 0).
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(*, S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0, 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(*, S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = "call") -> float:
    if T <= 0 or sigma <= 0:
        # At expiry, the price is intrinsic value.
        intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
        return intrinsic
    d1, d2 = _d1_d2(S=S, K=K, T=T, r=r, sigma=sigma)
    if option_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(*, S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = "call") -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(S=S, K=K, T=T, r=r, sigma=sigma)
    if option_type == "call":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def bs_gamma(*, S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1, _ = _d1_d2(S=S, K=K, T=T, r=r, sigma=sigma)
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def bs_theta(*, S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = "call") -> float:
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1, d2 = _d1_d2(S=S, K=K, T=T, r=r, sigma=sigma)
    term1 = -(S * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(T))
    if option_type == "call":
        return (term1 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    return (term1 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0


def bs_vega(*, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega per 1% change in volatility (so multiply by 0.01 for per-unit-sigma)."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1, _ = _d1_d2(S=S, K=K, T=T, r=r, sigma=sigma)
    return S * _norm_pdf(d1) * math.sqrt(T) / 100.0


__all__ = [
    "bs_delta", "bs_gamma", "bs_price", "bs_theta", "bs_vega",
]
