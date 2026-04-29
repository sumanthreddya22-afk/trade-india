from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.orchestrator import Decision, ScanResult
from trading_bot.reports import (
    build_vip_alert_email_html,
)


# --------------------------------------------------------------------------
# Test fixtures
# --------------------------------------------------------------------------


def _account(equity="15123.45", cash="12000", bp="24000", pv="15123.45"):
    return AccountSnapshot(
        equity=Decimal(equity),
        cash=Decimal(cash),
        buying_power=Decimal(bp),
        portfolio_value=Decimal(pv),
    )


def _position(symbol="AAPL", qty="3", mv="585", entry="195", pnl="12.50",
              asset_class="us_equity", current_price="195"):
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        market_value=Decimal(mv),
        avg_entry_price=Decimal(entry),
        current_price=Decimal(current_price),
        unrealized_pl=Decimal(pnl),
        asset_class=asset_class,
    )


def _scan_result(*decisions, ts=None):
    return ScanResult(
        decisions=list(decisions),
        timestamp=ts or datetime(2026, 4, 25, 20, 30, tzinfo=timezone.utc),
    )


# --------------------------------------------------------------------------
# Open Positions email (auto-protect summary)
# --------------------------------------------------------------------------


def _make_action(
    *, symbol="AAPL", qty="10", outcome="stop_placed",
    asset_class="stock", position_side="buy",
    stop_price=None, current_price=None, fill_estimate=None, error=None,
):
    from decimal import Decimal
    from trading_bot.alpaca_client import AssetClass, OrderSide
    from trading_bot.position_protection import ProtectionAction
    return ProtectionAction(
        symbol=symbol, qty=Decimal(qty),
        position_side=OrderSide(position_side),
        asset_class=AssetClass(asset_class),
        outcome=outcome,
        stop_price=stop_price, current_price=current_price,
        fill_estimate=fill_estimate, error=error,
    )


def test_open_positions_email_lists_protected_symbols():
    from trading_bot.reports import build_open_positions_email_html
    actions = [
        _make_action(symbol="AAPL", outcome="stop_placed",
                     stop_price=180.0, current_price=200.0),
        _make_action(symbol="MSFT", outcome="stop_placed",
                     stop_price=380.0, current_price=400.0),
    ]
    html = build_open_positions_email_html(actions, total_positions=5)
    assert "AAPL" in html
    assert "MSFT" in html
    assert "180.00" in html
    assert "Protected" in html


def test_open_positions_email_lists_closed_symbols():
    from trading_bot.reports import build_open_positions_email_html
    actions = [
        _make_action(symbol="XYZ", outcome="flattened", fill_estimate=12.34),
    ]
    html = build_open_positions_email_html(actions, total_positions=3)
    assert "XYZ" in html
    assert "Closed" in html
    assert "12.34" in html


def test_open_positions_email_lists_failures_and_deferred():
    from trading_bot.reports import build_open_positions_email_html
    actions = [
        _make_action(symbol="AAA", outcome="failed", error="rate limit"),
        _make_action(symbol="BBB", outcome="deferred_off_hours"),
    ]
    html = build_open_positions_email_html(actions, total_positions=2)
    assert "Failed" in html
    assert "rate limit" in html
    assert "Deferred" in html
    assert "BBB" in html


def test_open_positions_email_subject_clean_when_all_actioned():
    """No failures/deferred → subject is just 'Open Positions — N actioned'."""
    from trading_bot.reports import open_positions_email_subject
    actions = [
        _make_action(symbol="AAPL", outcome="stop_placed",
                     stop_price=180.0, current_price=200.0),
    ]
    subject = open_positions_email_subject(actions)
    assert subject == "Open Positions — 1 actioned"


def test_open_positions_email_subject_flags_attention_needed():
    """Any failed or deferred → 'N actioned, M need attention'."""
    from trading_bot.reports import open_positions_email_subject
    actions = [
        _make_action(symbol="AAPL", outcome="stop_placed",
                     stop_price=180.0, current_price=200.0),
        _make_action(symbol="BBB", outcome="failed", error="x"),
        _make_action(symbol="CCC", outcome="deferred_off_hours"),
    ]
    subject = open_positions_email_subject(actions)
    assert subject == "Open Positions — 1 actioned, 2 need attention"


# --------------------------------------------------------------------------
# VIP-tweet alert
# --------------------------------------------------------------------------


@dataclass
class _FakePost:
    severity: str
    handle: str
    platform: str
    text: str
    url: str
    severity_reason: str


def test_vip_alert_renders_each_post():
    posts = [
        _FakePost(
            severity="high",
            handle="@example",
            platform="truth_social",
            text="Major announcement about tariffs",
            url="https://truthsocial.example/123",
            severity_reason="keyword: tariffs",
        ),
    ]
    html = build_vip_alert_email_html(posts)
    assert "VIP Tweet Alert" in html
    assert "@example" in html
    assert "Major announcement" in html
    assert "truthsocial.example" in html
    assert "alert-only" in html
