"""Rotating file logger setup."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from trading_bot.daemon.logging_setup import setup_logging


def test_setup_creates_log_file(tmp_path: Path):
    log = tmp_path / "daemon.log"
    setup_logging(log_path=log, also_console=False)
    logging.getLogger("test").info("hello")
    # Force flush
    for h in logging.getLogger().handlers:
        h.flush()
    assert log.exists()
    assert "hello" in log.read_text()


def test_setup_idempotent_no_duplicate_handlers(tmp_path: Path):
    log = tmp_path / "daemon.log"
    setup_logging(log_path=log, also_console=False)
    setup_logging(log_path=log, also_console=False)
    # Only one rotating handler should remain.
    handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(handlers) == 1


def test_setup_silences_noisy_libs(tmp_path: Path):
    setup_logging(log_path=tmp_path / "daemon.log", also_console=False)
    assert logging.getLogger("apscheduler").level == logging.WARNING
    assert logging.getLogger("urllib3").level == logging.WARNING
