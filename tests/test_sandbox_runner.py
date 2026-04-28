"""Sandbox runtime tests."""
import pytest

from trading_bot.sandbox_runner import run_in_sandbox


def test_passing_test_module():
    src = "def add(a, b): return a + b"
    test_src = """
import sys
sys.path.insert(0, '.')
from mod import add

def test_add():
    assert add(2, 3) == 5
"""
    result = run_in_sandbox(
        module_name="mod", source=src, test_source=test_src, walltime_s=15
    )
    assert result.passed, f"unexpected failure: {result.stdout}\n{result.stderr}"


def test_failing_test_module():
    src = "def add(a, b): return a + b + 1  # bug"
    test_src = """
import sys
sys.path.insert(0, '.')
from mod import add

def test_add(): assert add(2, 3) == 5
"""
    result = run_in_sandbox(
        module_name="mod", source=src, test_source=test_src, walltime_s=15
    )
    assert not result.passed


@pytest.mark.slow
def test_infinite_loop_killed_by_timeout():
    src = "def loop():\n    while True: pass"
    test_src = """
import sys
sys.path.insert(0, '.')
from mod import loop

def test_loop(): loop()
"""
    result = run_in_sandbox(
        module_name="mod", source=src, test_source=test_src, walltime_s=3
    )
    assert not result.passed
    assert result.timed_out
