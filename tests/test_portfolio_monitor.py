from pathlib import Path

from trading_bot.portfolio_monitor import (
    PositionSnap,
    Snapshot,
    diff_snapshots,
    has_alerts,
    load_snapshot,
    save_snapshot,
)


def _snap(equity: str, positions: dict) -> Snapshot:
    return Snapshot(taken_at="2026-04-25T12:30:00+00:00", equity=equity, positions=positions)


def test_diff_no_prev_returns_init_event():
    events = diff_snapshots(None, _snap("15000", {}))
    assert len(events) == 1
    assert events[0].kind == "init"


def test_diff_detects_new_position():
    prev = _snap("15000", {})
    curr = _snap("15000", {"AMD": PositionSnap("AMD", "3", "660", "220", "0")})
    events = diff_snapshots(prev, curr)
    new_pos = [e for e in events if e.kind == "new_position"]
    assert len(new_pos) == 1
    assert new_pos[0].symbol == "AMD"
    assert new_pos[0].severity == "alert"


def test_diff_detects_closed_position():
    prev = _snap("15000", {"AMD": PositionSnap("AMD", "3", "660", "220", "20")})
    curr = _snap("15020", {})
    events = diff_snapshots(prev, curr)
    closed = [e for e in events if e.kind == "closed_position"]
    assert len(closed) == 1
    assert closed[0].symbol == "AMD"


def test_diff_detects_big_unrealized_move():
    prev = _snap("15000", {"AMD": PositionSnap("AMD", "3", "660", "220", "0")})
    # 3% move on $660 = $19.80 P&L change → exceeds 2% threshold
    curr = _snap("15020", {"AMD": PositionSnap("AMD", "3", "680", "220", "20")})
    events = diff_snapshots(prev, curr, big_move_pct_threshold=2.0)
    moves = [e for e in events if e.kind == "unrealized_move"]
    assert len(moves) >= 1


def test_diff_ignores_small_moves():
    prev = _snap("15000", {"AMD": PositionSnap("AMD", "3", "660", "220", "0")})
    # 0.5% move — under threshold
    curr = _snap("15003", {"AMD": PositionSnap("AMD", "3", "663", "220", "3")})
    events = diff_snapshots(prev, curr, big_move_pct_threshold=2.0)
    moves = [e for e in events if e.kind == "unrealized_move"]
    assert len(moves) == 0


def test_diff_detects_equity_drop():
    prev = _snap("15000", {})
    # 3% drop
    curr = _snap("14550", {})
    events = diff_snapshots(prev, curr, big_move_pct_threshold=2.0)
    eq_moves = [e for e in events if e.kind == "equity_move"]
    assert len(eq_moves) == 1
    assert eq_moves[0].severity == "alert"


def test_save_and_load_snapshot(tmp_path: Path):
    p = tmp_path / "snap.json"
    snap = _snap("15000", {"AMD": PositionSnap("AMD", "3", "660", "220", "0")})
    save_snapshot(p, snap)
    loaded = load_snapshot(p)
    assert loaded is not None
    assert loaded.equity == "15000"
    assert "AMD" in loaded.positions


def test_load_missing_returns_none(tmp_path: Path):
    assert load_snapshot(tmp_path / "missing.json") is None


def test_has_alerts():
    from trading_bot.portfolio_monitor import Event
    assert has_alerts([Event("alert", "x", "Y", "msg")])
    assert not has_alerts([Event("info", "x", "Y", "msg")])
