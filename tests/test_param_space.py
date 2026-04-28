"""Param search space registry tests."""
from trading_bot.param_space import PARAM_SPACE


def test_momentum_space_complete():
    momentum = PARAM_SPACE["momentum"]
    assert set(momentum.keys()) >= {
        "rsi_lower",
        "rsi_upper",
        "ema_period",
        "stop_pct",
        "sentiment_floor",
    }


def test_unknown_template_returns_empty():
    assert PARAM_SPACE.get("unknown", {}) == {}


def test_each_entry_is_low_high_kind_tuple():
    for template, space in PARAM_SPACE.items():
        for name, spec in space.items():
            assert len(spec) == 3, f"{template}.{name} not (low, high, kind)"
            low, high, kind = spec
            assert kind in {"int", "float"}
            assert low < high
