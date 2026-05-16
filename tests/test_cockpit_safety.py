"""WS5f Layer 4 — PAUSE / FLATTEN operator controls + precheck integration."""
from __future__ import annotations

import sqlite3

import pytest

from trading_bot.ledger.manual_halt_event import (
    current_pause_state, write_event,
)
from trading_bot.ledger.schema import create_ledger


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_ledger(conn)
    return conn


def test_pause_resume_lifecycle() -> None:
    conn = _conn()
    assert current_pause_state(conn) == "normal"
    write_event(conn, action="pause", operator="op", source="cli",
                reason="testing")
    assert current_pause_state(conn) == "paused"
    write_event(conn, action="resume", operator="op", source="cli",
                reason="done testing")
    assert current_pause_state(conn) == "normal"


def test_flatten_is_terminal_state() -> None:
    conn = _conn()
    write_event(conn, action="flatten", operator="op", source="cli",
                reason="emergency")
    assert current_pause_state(conn) == "flattened"


def test_write_event_validates_required_fields() -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        write_event(conn, action="invalid", operator="op", source="cli")
    with pytest.raises(ValueError):
        write_event(conn, action="pause", operator="", source="cli")
    with pytest.raises(ValueError):
        write_event(conn, action="pause", operator="op", source="invalid")


def test_pause_blocks_new_entries_in_precheck() -> None:
    from trading_bot.ledger.order_master import OrderIntent
    from trading_bot.risk import load_policy, precheck
    from trading_bot.risk.types import AccountState
    bundle = load_policy()
    conn = _conn()
    write_event(conn, action="pause", operator="op", source="cli",
                reason="test")
    intent = OrderIntent(
        client_order_id="t1", strategy_id="ETF_MOMENTUM_v1", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="buy", qty=1,
        limit_price=400.0, tif="day", origin="strategy",
    )
    acct = AccountState(
        equity=1000, cash=500, equity_at_session_start=1000, day_trade_count=0,
    )
    d = precheck.evaluate(
        conn=conn, intent=intent, account=acct, positions=[],
        policy=bundle, lane="benchmark", intent_price=400.0,
    )
    assert d.verdict == "halt"
    assert "manual_halt:paused" in d.reason


def test_pause_does_not_block_exits() -> None:
    from trading_bot.ledger.order_master import OrderIntent
    from trading_bot.risk import load_policy, precheck
    from trading_bot.risk.types import AccountState
    bundle = load_policy()
    conn = _conn()
    write_event(conn, action="pause", operator="op", source="cli",
                reason="test")
    intent = OrderIntent(
        client_order_id="t2", strategy_id="ETF_MOMENTUM_v1", strategy_ver=1,
        symbol="SPY", asset_class="equity", side="sell_to_close", qty=1,
        limit_price=400.0, tif="day", origin="strategy",
    )
    acct = AccountState(
        equity=1000, cash=500, equity_at_session_start=1000, day_trade_count=0,
    )
    d = precheck.evaluate(
        conn=conn, intent=intent, account=acct, positions=[],
        policy=bundle, lane="etf_momentum", intent_price=400.0,
    )
    # Not blocked by manual_halt; may halt for other reasons (lane status,
    # etc.) but reason will NOT contain manual_halt.
    if d.verdict == "halt":
        assert "manual_halt" not in d.reason
