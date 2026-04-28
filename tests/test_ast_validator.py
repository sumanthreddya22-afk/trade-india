"""AST validator tests."""
from trading_bot.ast_validator import validate_ast


def test_clean_module_passes():
    src = """
import pandas as pd
import numpy as np
from typing import Any

def evaluate(x: int) -> int:
    return x * 2
"""
    r = validate_ast(src)
    assert r.passes
    assert r.forbidden_imports == []


def test_forbidden_import_caught():
    src = """
import os
def evaluate(x): return x
"""
    r = validate_ast(src)
    assert not r.passes
    assert "os" in r.forbidden_imports


def test_forbidden_eval_caught():
    src = """
def evaluate(x): return eval('x + 1')
"""
    r = validate_ast(src)
    assert not r.passes
    assert "eval" in r.forbidden_calls


def test_forbidden_subprocess_caught():
    src = "import subprocess\nsubprocess.run(['ls'])"
    r = validate_ast(src)
    assert not r.passes


def test_syntax_error_caught():
    src = "def broken(:"
    r = validate_ast(src)
    assert not r.passes
    assert r.syntax_error is not None


def test_dynamic_import_caught():
    src = "x = __import__('os')"
    r = validate_ast(src)
    assert not r.passes
    assert "__import__" in r.forbidden_calls
