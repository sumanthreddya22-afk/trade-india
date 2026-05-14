"""Dashboard /digest route + enriched status page."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "ledger").mkdir(parents=True)
    from trading_bot.ledger import connect_writer, create_ledger
    p = tmp_path / "data" / "ledger" / "ledger.db"
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    from trading_bot.operator_ui.app import app as fastapi_app
    return TestClient(fastapi_app)


def test_digest_route_renders(client):
    r = client.get("/digest")
    assert r.status_code == 200
    assert "Digest" in r.text
    assert "Heartbeats" in r.text


def test_api_digest_returns_json(client):
    r = client.get("/api/digest?hours=12")
    assert r.status_code == 200
    body = r.json()
    assert body["window_hours"] == 12


def test_status_includes_account_section(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Account" in r.text
    assert "Current positions" in r.text


def test_status_auto_refresh_meta(client):
    r = client.get("/")
    assert "http-equiv=\"refresh\"" in r.text
