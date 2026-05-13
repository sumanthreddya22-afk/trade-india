"""P0 acceptance: position classifier covers all four cases.

Plan v4 §0: every open position must classify as bot|external|manual|unknown
before any new entry is allowed. Phase 0 ships the function; the runtime
halt-on-unknown gate lands in Phase 2.
"""
from __future__ import annotations

from trading_bot.position_classifier import (
    BrokerPosition,
    OrderMasterRow,
    classify,
    looks_like_bot_client_order_id,
)


def test_bot_via_v4_client_order_id_pattern() -> None:
    pos = BrokerPosition(
        symbol="SPY",
        client_order_id="20260513_ETF_MOMENTUM_SPY_42",
    )
    assert classify(pos) == "bot"


def test_bot_via_legacy_prefix() -> None:
    pos = BrokerPosition(
        symbol="QQQ",
        client_order_id="trading-bot-7f3a",
    )
    assert classify(pos) == "bot"


def test_external_when_id_present_but_alien() -> None:
    pos = BrokerPosition(
        symbol="AAPL",
        client_order_id="webull-9c2",
    )
    assert classify(pos) == "external"


def test_manual_origin_wins_over_id_pattern() -> None:
    pos = BrokerPosition(
        symbol="SPY",
        client_order_id="20260513_ETF_MOMENTUM_SPY_42",
        origin="manual",
    )
    assert classify(pos) == "manual"


def test_unknown_when_no_id() -> None:
    pos = BrokerPosition(symbol="GLD")
    assert classify(pos) == "unknown"


def test_lookup_overrides_id_classification_to_bot() -> None:
    pos = BrokerPosition(symbol="IWM", client_order_id="legacy-abc")

    def lookup(cid: str):
        return OrderMasterRow(client_order_id=cid, origin="strategy", symbol="IWM")

    assert classify(pos, order_master_lookup=lookup) == "bot"


def test_lookup_overrides_id_classification_to_manual() -> None:
    pos = BrokerPosition(symbol="EFA", client_order_id="op-1")

    def lookup(cid: str):
        return OrderMasterRow(client_order_id=cid, origin="manual", symbol="EFA")

    assert classify(pos, order_master_lookup=lookup) == "manual"


def test_helper_predicate() -> None:
    assert looks_like_bot_client_order_id("20260513_LANE_SPY_1")
    assert looks_like_bot_client_order_id("wheel-runner-7")
    assert not looks_like_bot_client_order_id("manual-trade")
    assert not looks_like_bot_client_order_id("")
    assert not looks_like_bot_client_order_id(None)
