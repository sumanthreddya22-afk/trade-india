import json
import datetime as dt
from pathlib import Path

import pytest

from trading_bot.log_structured import StructuredLogger, get_run_path


def test_get_run_path_format(tmp_path):
    p = get_run_path(
        base=tmp_path,
        date=dt.date(2026, 4, 28),
        role="stock_scanner",
        ts=dt.datetime(2026, 4, 28, 10, 0, 14, tzinfo=dt.timezone.utc),
    )
    assert p == tmp_path / "2026-04-28" / "stock_scanner" / "10-00-14.json"


def test_logger_writes_json_event(tmp_path):
    log = StructuredLogger(base=tmp_path, role="stock_scanner")
    log.event("scan_start", symbols=25, regime="trending_up")
    log.event("scan_finish", placed=1, vetoed=2)

    files = list((tmp_path / dt.datetime.now(dt.timezone.utc).date().isoformat() / "stock_scanner").glob("*.json"))
    assert len(files) == 2
    payload = json.loads(files[0].read_text())
    assert "ts" in payload
    assert payload["role"] == "stock_scanner"
    # First file written is the first event call
    assert payload["event"] in {"scan_start", "scan_finish"}


def test_logger_event_includes_arbitrary_kwargs(tmp_path):
    log = StructuredLogger(base=tmp_path, role="stock_scanner")
    log.event("decision", symbol="AAPL", action="buy", conviction=0.82)
    files = sorted((tmp_path / dt.datetime.now(dt.timezone.utc).date().isoformat() / "stock_scanner").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["symbol"] == "AAPL"
    assert payload["action"] == "buy"
    assert payload["conviction"] == 0.82


def test_logger_event_handles_exception(tmp_path):
    log = StructuredLogger(base=tmp_path, role="stock_scanner")
    try:
        raise ValueError("boom")
    except ValueError as e:
        log.error("scan_failed", error=e)
    files = list((tmp_path / dt.datetime.now(dt.timezone.utc).date().isoformat() / "stock_scanner").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["event"] == "scan_failed"
    assert payload["error_type"] == "ValueError"
    assert "boom" in payload["error_message"]
    assert "Traceback" in payload["traceback"]
