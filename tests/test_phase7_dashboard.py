"""Dashboard FastAPI route tests with TestClient."""
from __future__ import annotations

from pathlib import Path

import pytest

from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # Point cwd at a tmp dir with an initialised ledger so the dashboard
    # reads from somewhere predictable.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "ledger").mkdir(parents=True)
    # Touch a real ledger DB so status_snapshot returns ledger_present=True.
    from trading_bot.ledger import connect_writer, create_ledger
    p = tmp_path / "data" / "ledger" / "ledger.db"
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()

    from trading_bot.operator_ui.app import app as fastapi_app
    return TestClient(fastapi_app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_home_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "operator dashboard" in r.text
    assert "Daemon heartbeats" in r.text


def test_api_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "active_kills" in body
    assert "halted" in body


def test_risk_page_renders(client):
    r = client.get("/risk")
    assert r.status_code == 200
    assert "Risk profile" in r.text
    assert "safe" in r.text and "aggressive" in r.text


def test_halt_page_renders(client):
    r = client.get("/halt")
    assert r.status_code == 200
    assert "Manual halt" in r.text


def test_halt_then_resume_via_form(client):
    r = client.post("/halt/halt", data={"reason": "test"})
    assert r.status_code == 200
    assert "Halted: seq=" in r.text

    r = client.post("/halt/resume", data={"reason": "done"})
    assert r.status_code == 200
    assert "Resumed: seq=" in r.text


def test_strategy_page_renders(client):
    r = client.get("/strategy")
    assert r.status_code == 200
    assert "Submit a new strategy" in r.text


def test_strategy_submit_draft(client):
    r = client.post(
        "/strategy/submit",
        data={"name": "TestStrat", "description": "buy low sell high", "mode": "draft"},
    )
    assert r.status_code == 200
    assert "Submission result" in r.text
    assert "TESTSTRAT_" in r.text
