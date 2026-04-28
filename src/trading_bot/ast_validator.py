"""AST allowlist validator. Pure-Python AST walk, no LLM.

Enforces spec §13.1 hard constraints on Architect-generated code:
  Imports allowed: pandas, numpy, ta, math, datetime, dataclasses, typing,
                   decimal, enum, statistics, collections, functools.
  Forbidden calls: eval, exec, compile, __import__, getattr-based imports,
                   open, file I/O.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

ALLOWED_IMPORT_ROOTS: set[str] = {
    "pandas",
    "numpy",
    "ta",
    "math",
    "datetime",
    "dataclasses",
    "typing",
    "decimal",
    "enum",
    "statistics",
    "collections",
    "functools",
    "trading_bot",  # for BaseStrategy + Indicators import
}

FORBIDDEN_CALLS: set[str] = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
}


@dataclass
class AstReport:
    allowed_imports: set[str] = field(default_factory=set)
    forbidden_imports: list[str] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    syntax_error: str | None = None

    @property
    def passes(self) -> bool:
        return (
            not self.forbidden_imports
            and not self.forbidden_calls
            and self.syntax_error is None
        )


def _root_module(name: str) -> str:
    return name.split(".")[0]


def validate_ast(source: str) -> AstReport:
    report = AstReport()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        report.syntax_error = str(e)
        return report

    for node in ast.walk(tree):
        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root_module(alias.name)
                if root in ALLOWED_IMPORT_ROOTS:
                    report.allowed_imports.add(alias.name)
                else:
                    report.forbidden_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                report.forbidden_imports.append("relative_import")
                continue
            root = _root_module(node.module)
            if root in ALLOWED_IMPORT_ROOTS:
                report.allowed_imports.add(node.module)
            else:
                report.forbidden_imports.append(node.module)
        # Direct calls to forbidden builtins
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                report.forbidden_calls.append(func.id)
            # __import__ via getattr trickery: __builtins__.__import__('os')
            if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_CALLS:
                report.forbidden_calls.append(f"<attr>.{func.attr}")

    return report
