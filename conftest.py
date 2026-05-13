"""Root conftest — put ``src`` on the import path for the test session.

The repo follows the ``src`` layout (``pyproject.toml`` declares
``[tool.setuptools.packages.find] where = ["src"]``) but the package
isn't installed into the venv as an editable. Tests have historically
required ``PYTHONPATH=src`` at the command line; this conftest removes
that friction so ``pytest`` works from a clean checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
