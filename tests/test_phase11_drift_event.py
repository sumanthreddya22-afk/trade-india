"""drift_event ledger table + drift_monitor wiring (Plan v4 §9)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from trading_bot.daemon import jobs
from trading_bot.daemon.jobs import DaemonContext
from trading_bot.ledger import connect_writer, create_ledger
from trading_bot.ledger.drift_event import latest_for_lane, write_event
from trading_bot.ledger.hash_chain import verify_chain


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    p = tmp_path / "ledger.db"
    conn = connect_writer(p)
    create_ledger(conn)
    conn.close()
    return p


def test_write_event_roundtrips_and_extends_chain(ledger) -> None:
    conn = connect_writer(ledger)
    try:
        write_event(
            conn, lane="equity", n_trades=12,
            modelled_mean_bps=5.0, realised_mean_bps=4.5, ratio=0.9,
            tolerance_multiplier=2.0, breach=False, recommendation="",
        )
        write_event(
            conn, lane="crypto", n_trades=8,
            modelled_mean_bps=10.0, realised_mean_bps=25.0, ratio=2.5,
            tolerance_multiplier=2.0, breach=True,
            recommendation="demote:crypto",
        )
        conn.commit()

        cur = conn.execute(
            "SELECT lane, breach, recommendation FROM drift_event "
            "ORDER BY ledger_seq"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    assert rows == [("equity", 0, ""), ("crypto", 1, "demote:crypto")]

    # Chain integrity must hold across both rows.
    conn = sqlite3.connect(str(ledger))
    try:
        n = verify_chain(conn, "drift_event")
    finally:
        conn.close()
    assert n == 2


def test_drift_event_is_append_only(ledger) -> None:
    conn = connect_writer(ledger)
    try:
        write_event(
            conn, lane="equity", n_trades=1, modelled_mean_bps=5.0,
            realised_mean_bps=5.0, ratio=1.0, tolerance_multiplier=2.0,
            breach=False, recommendation="",
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE drift_event SET ratio=99 WHERE lane='equity'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM drift_event")
    finally:
        conn.close()


def test_latest_for_lane_returns_most_recent(ledger) -> None:
    conn = connect_writer(ledger)
    try:
        write_event(
            conn, lane="equity", n_trades=1, modelled_mean_bps=5.0,
            realised_mean_bps=5.0, ratio=1.0, tolerance_multiplier=2.0,
            breach=False, recommendation="",
        )
        write_event(
            conn, lane="equity", n_trades=5, modelled_mean_bps=5.0,
            realised_mean_bps=12.0, ratio=2.4, tolerance_multiplier=2.0,
            breach=True, recommendation="demote:equity",
        )
        conn.commit()
        out = latest_for_lane(conn, "equity")
    finally:
        conn.close()
    assert out is not None
    assert out["breach"] is True
    assert out["recommendation"] == "demote:equity"
    assert out["n_trades"] == 5
    assert latest_for_lane(sqlite3.connect(str(ledger)), "missing") is None


def test_job_drift_monitor_writes_event_per_lane(ledger, tmp_path) -> None:
    """Without any fills, the monitor still appends a zero-trade row
    per lane — proof the job ran. Alerts are NOT fired (no breach)."""
    mirror = tmp_path / "mirror.db"
    from trading_bot.ledger import init_mirror
    init_mirror(mirror)
    ctx = DaemonContext(
        ledger_db=ledger, mirror_db=mirror,
        policy_dir=Path(__file__).resolve().parent.parent / "policy",
    )
    with mock.patch("trading_bot.obs.notifier.send_drift_alert") as alert:
        jobs.job_drift_monitor(ctx)
    alert.assert_not_called()

    conn = sqlite3.connect(str(ledger))
    try:
        cur = conn.execute(
            "SELECT lane, n_trades, breach FROM drift_event ORDER BY lane"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    lanes = {r[0] for r in rows}
    assert lanes == {"equity", "crypto"}
    assert all(r[1] == 0 and r[2] == 0 for r in rows)


def test_job_drift_monitor_alerts_on_breach(ledger, tmp_path) -> None:
    """When compute_drift returns a breaching report, the daemon
    persists it AND fires a drift alert. Notifier is mocked so the
    test never hits SMTP."""
    mirror = tmp_path / "mirror.db"
    from trading_bot.ledger import init_mirror
    init_mirror(mirror)
    ctx = DaemonContext(
        ledger_db=ledger, mirror_db=mirror,
        policy_dir=Path(__file__).resolve().parent.parent / "policy",
    )

    from trading_bot.execution.drift_monitor import DriftReport
    breach_equity = DriftReport(
        lane="equity", n_trades=20, modelled_mean_bps=5.0,
        realised_mean_bps=12.0, ratio=2.4, breach=True,
        recommendation="demote:equity",
    )
    clean_crypto = DriftReport(
        lane="crypto", n_trades=4, modelled_mean_bps=10.0,
        realised_mean_bps=10.0, ratio=1.0, breach=False,
        recommendation="",
    )

    def fake_compute(*args, **kwargs):
        return breach_equity if kwargs.get("lane") == "equity" else clean_crypto

    with mock.patch("trading_bot.execution.drift_monitor.compute_drift",
                    side_effect=fake_compute), \
         mock.patch("trading_bot.obs.notifier.send_drift_alert") as alert:
        jobs.job_drift_monitor(ctx)

    # One alert, for the equity lane.
    assert alert.call_count == 1
    kwargs = alert.call_args.kwargs
    assert kwargs["lane"] == "equity"
    assert kwargs["ratio"] == pytest.approx(2.4)
    assert kwargs["recommendation"] == "demote:equity"

    # Both lanes are persisted regardless.
    conn = sqlite3.connect(str(ledger))
    try:
        cur = conn.execute(
            "SELECT lane, breach FROM drift_event ORDER BY lane"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    assert ("crypto", 0) in rows
    assert ("equity", 1) in rows


def test_send_drift_alert_no_op_without_creds(monkeypatch) -> None:
    """The notifier path must not raise when GMAIL creds are unset —
    the daemon depends on that for headless dev."""
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    from trading_bot.obs.notifier import send_drift_alert
    out = send_drift_alert(
        lane="equity", n_trades=20, modelled_mean_bps=5.0,
        realised_mean_bps=12.0, ratio=2.4,
        recommendation="demote:equity",
    )
    assert out["ok"] is False
    assert out["reason"] == "creds_missing"
