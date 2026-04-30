import pytest
from trading_bot.state_pause import (
    clear_halted_strategy,
    clear_pause,
    is_paused,
    read_halted_strategies,
    set_halted_strategy,
    set_pause,
)


@pytest.fixture
def flag_path(tmp_path):
    return tmp_path / "pause.flag"


def test_is_paused_false_when_no_flag(flag_path):
    assert is_paused(flag_path) is False


def test_set_pause_creates_flag_with_reason(flag_path):
    set_pause(flag_path, reason="drawdown breach 21.4%")
    assert is_paused(flag_path) is True
    assert "drawdown breach 21.4%" in flag_path.read_text()


def test_clear_pause_removes_flag(flag_path):
    set_pause(flag_path, reason="test")
    clear_pause(flag_path)
    assert is_paused(flag_path) is False


def test_clear_pause_idempotent(flag_path):
    clear_pause(flag_path)  # already absent — should not raise
    clear_pause(flag_path)
    assert is_paused(flag_path) is False


# ---- Bucket A: per-strategy halt file ----


@pytest.fixture
def halted_path(tmp_path):
    return tmp_path / "halted_strategies.txt"


def test_read_halted_returns_empty_when_missing(halted_path):
    assert read_halted_strategies(halted_path) == frozenset()


def test_set_and_read_halted_strategy(halted_path):
    set_halted_strategy(halted_path, "wheel")
    assert read_halted_strategies(halted_path) == frozenset({"wheel"})


def test_set_halted_strategy_is_idempotent(halted_path):
    set_halted_strategy(halted_path, "wheel")
    set_halted_strategy(halted_path, "wheel")
    assert read_halted_strategies(halted_path) == frozenset({"wheel"})


def test_set_multiple_strategies(halted_path):
    set_halted_strategy(halted_path, "wheel")
    set_halted_strategy(halted_path, "momentum")
    assert read_halted_strategies(halted_path) == frozenset({"wheel", "momentum"})


def test_clear_halted_strategy_removes_one(halted_path):
    set_halted_strategy(halted_path, "wheel")
    set_halted_strategy(halted_path, "momentum")
    clear_halted_strategy(halted_path, "wheel")
    assert read_halted_strategies(halted_path) == frozenset({"momentum"})


def test_clear_last_halted_removes_file(halted_path):
    set_halted_strategy(halted_path, "wheel")
    clear_halted_strategy(halted_path, "wheel")
    assert not halted_path.exists()


def test_read_halted_skips_blanks_and_comments(halted_path):
    halted_path.write_text("# operator paused this lane\nwheel\n\n# next:\n  momentum  \n")
    assert read_halted_strategies(halted_path) == frozenset({"wheel", "momentum"})
