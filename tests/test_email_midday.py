import datetime as dt
from decimal import Decimal


def _ctx(**o):
    from trading_bot.email_midday import SnapshotContext
    base = dict(
        as_of=dt.datetime(2026, 4, 28, 12, 0, tzinfo=dt.timezone.utc),
        equity=Decimal("14962.00"),
        starting_equity=Decimal("14984.16"),
        realized_pnl_today=Decimal("0"),
        unrealized_pnl=Decimal("-22.16"),
        regime="trending_up",
        positions=[],
        trades_today=[],
        watchlist_signals=[],
        daily_loss_pct=0.15,
        drawdown_pct=2.1,
        version="phase4-v1",
        git_sha="abc123",
    )
    base.update(o)
    return SnapshotContext(**base)


def test_midday_subject_format():
    from trading_bot.email_midday import build_midday_snapshot_email
    e = build_midday_snapshot_email(_ctx())
    assert "Midday Snapshot" in e.subject
    assert "Apr 28" in e.subject


def test_midday_renders_kpi():
    from trading_bot.email_midday import build_midday_snapshot_email
    e = build_midday_snapshot_email(_ctx())
    assert "$14,962" in e.html_body or "14,962" in e.html_body


def test_midday_watchlist_signals_render():
    from trading_bot.email_midday import build_midday_snapshot_email
    e = build_midday_snapshot_email(_ctx(
        watchlist_signals=[{"symbol": "AMD", "distance_to_trigger_pct": 1.4,
                            "note": "RSI 54.0, needs >=55"}],
    ))
    assert "AMD" in e.html_body
    assert "1.4" in e.html_body
