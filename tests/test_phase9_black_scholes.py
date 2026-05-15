"""Black-Scholes Greeks sanity tests."""
from __future__ import annotations

import math

import pytest

from trading_bot.shared.black_scholes import (
    bs_delta, bs_gamma, bs_price, bs_theta, bs_vega,
)


def test_atm_call_delta_around_half():
    d = bs_delta(S=100, K=100, T=30 / 365, r=0.045, sigma=0.20, option_type="call")
    assert 0.50 <= d <= 0.55   # slightly > 0.5 with positive r


def test_atm_put_delta_around_minus_half():
    d = bs_delta(S=100, K=100, T=30 / 365, r=0.045, sigma=0.20, option_type="put")
    assert -0.50 <= d <= -0.40


def test_otm_put_low_delta_magnitude():
    """A 0.30-delta put should sit below spot by a few %."""
    d = bs_delta(S=400, K=380, T=30 / 365, r=0.045, sigma=0.20, option_type="put")
    assert 0.15 <= abs(d) <= 0.45


def test_call_put_parity_atm():
    """C - P = S - K*e^(-rT) at the same strike."""
    S, K, T, r, sigma = 100, 100, 30 / 365, 0.045, 0.20
    c = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, option_type="call")
    p = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, option_type="put")
    parity = S - K * math.exp(-r * T)
    assert abs((c - p) - parity) < 0.01


def test_zero_time_returns_intrinsic():
    """At expiry the price is max(S-K, 0) for calls, max(K-S, 0) for puts."""
    assert bs_price(S=110, K=100, T=0, r=0.045, sigma=0.20, option_type="call") == 10
    assert bs_price(S=90, K=100, T=0, r=0.045, sigma=0.20, option_type="put") == 10


def test_gamma_nonnegative():
    g = bs_gamma(S=100, K=100, T=30 / 365, r=0.045, sigma=0.20)
    assert g >= 0


def test_theta_negative_for_long_options():
    """Long options decay daily."""
    t_call = bs_theta(S=100, K=100, T=30 / 365, r=0.045, sigma=0.20, option_type="call")
    t_put = bs_theta(S=100, K=100, T=30 / 365, r=0.045, sigma=0.20, option_type="put")
    assert t_call < 0
    assert t_put < 0


def test_vega_positive():
    v = bs_vega(S=100, K=100, T=30 / 365, r=0.045, sigma=0.20)
    assert v > 0
