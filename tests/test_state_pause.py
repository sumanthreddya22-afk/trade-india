import pytest
from trading_bot.state_pause import is_paused, set_pause, clear_pause


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
