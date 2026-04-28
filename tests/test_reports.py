from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.orchestrator import Decision, ScanResult
from trading_bot.portfolio_monitor import Event
from trading_bot.reports import (
    build_alert_email_html,
    build_daily_report_html,
    build_rich_report_html,
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
              asset_class="us_equity"):
    return Position(
        symbol=symbol,
        qty=Decimal(qty),
        market_value=Decimal(mv),
        avg_entry_price=Decimal(entry),
        unrealized_pl=Decimal(pnl),
        asset_class=asset_class,
    )


def _scan_result(*decisions, ts=None):
    return ScanResult(
        decisions=list(decisions),
        timestamp=ts or datetime(2026, 4, 25, 20, 30, tzinfo=timezone.utc),
    )


# --------------------------------------------------------------------------
# Daily report
# --------------------------------------------------------------------------


def test_daily_report_contains_account_and_decisions():
    """Smoke test: data flows through and shows up in the rendered HTML."""
    account = _account()
    positions = [_position()]
    scan = _scan_result(
        Decision(symbol="MSFT", action="placed_order",
                 reason="rsi=58.0 macd>0.020 close>EMA20",
                 entry_order_id="e-1", stop_loss_order_id="s-1"),
        Decision(symbol="QQQ", action="hold", reason="rsi 45.2 outside [55, 70]"),
        Decision(symbol="SPY", action="skipped_existing_position"),
    )

    html = build_daily_report_html(
        account=account, positions=positions, scan=scan,
        spy_daily_change_pct=Decimal("1.20"),
        regime="trending_up",
    )
    # Equity is formatted with thousands separator.
    assert "15,123.45" in html
    # Position + decision symbols are surfaced.
    assert "AAPL" in html
    assert "MSFT" in html
    assert "QQQ" in html
    assert "SPY" in html
    # Action labels render humanised (underscores → spaces).
    assert "placed order" in html
    # Regime renders humanised inside the regime pill.
    assert "trending up" in html
    # SPY daily change shown.
    assert "1.20" in html


def test_daily_report_handles_empty_state():
    html = build_daily_report_html(
        account=_account(),
        positions=[],
        scan=_scan_result(),
        spy_daily_change_pct=Decimal("0"),
        regime="sideways",
    )
    assert "No open positions" in html
    assert "No decisions in this run" in html
    assert "sideways" in html


def test_daily_report_pnl_color_coding():
    """Positive P&L renders the green token; negative renders the red token."""
    pos_winner = _position(symbol="WIN", pnl="100.00")
    pos_loser = _position(symbol="LOSE", pnl="-50.00", mv="500", entry="200")
    html = build_daily_report_html(
        account=_account(),
        positions=[pos_winner, pos_loser],
        scan=_scan_result(),
        spy_daily_change_pct=Decimal("0"),
        regime="trending_up",
    )
    # Green & red design tokens both appear (one per position).
    assert "#34d399" in html  # GOOD
    assert "#fb7185" in html  # BAD


# --------------------------------------------------------------------------
# Rich report
# --------------------------------------------------------------------------


@pytest.fixture
def fake_intel():
    """Minimal IntelligenceBundle stand-in. The builder accesses these
    attributes by name; we use a SimpleNamespace-like duck type."""
    from types import SimpleNamespace
    macro = SimpleNamespace(
        vix=18.5,
        yield_10y_pct=4.25,
        fed_funds_pct=4.50,
        fetched_at=datetime(2026, 4, 25, 20, 30, tzinfo=timezone.utc),
    )
    return SimpleNamespace(
        macro=macro,
        news_by_symbol={},
        gdelt=[],
        insider=[],
    )


def test_rich_report_includes_macro(fake_intel):
    html = build_rich_report_html(
        period="mid",
        account=_account(),
        positions=[_position()],
        scan=_scan_result(),
        spy_daily_change_pct=Decimal("0.50"),
        regime="trending_up",
        intel=fake_intel,
    )
    assert "Mid-Day Report" in html
    assert "Macro Snapshot" in html
    assert "18.50" in html  # VIX
    assert "4.25" in html   # 10Y
    assert "calm" in html   # VIX < 22 → "calm" pill


def test_rich_report_high_vix_triggers_warn_pill(fake_intel):
    fake_intel.macro.vix = 32.0
    html = build_rich_report_html(
        period="eod",
        account=_account(),
        positions=[],
        scan=_scan_result(),
        spy_daily_change_pct=Decimal("-1.50"),
        regime="risk_off",
        intel=fake_intel,
    )
    assert "End-of-Day Report" in html
    assert "elevated" in html
    assert "risk off" in html  # regime humanised


def test_rich_report_renders_events(fake_intel):
    events = [
        Event(severity="alert", kind="new_position", symbol="NVDA",
              message="NEW position: NVDA qty=10"),
        Event(severity="info", kind="qty_change", symbol="AAPL",
              message="AAPL qty changed: 3 → 4"),
    ]
    html = build_rich_report_html(
        period="mid", account=_account(), positions=[], scan=_scan_result(),
        spy_daily_change_pct=Decimal("0"), regime="sideways",
        intel=fake_intel, events=events,
    )
    assert "Portfolio Events" in html
    assert "NVDA" in html
    assert "new_position" in html


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
# Portfolio-watch alert
# --------------------------------------------------------------------------


def test_alert_email_renders_severity_pills():
    events = [
        Event(severity="alert", kind="equity_move", symbol="",
              message="Equity moved -3.20%"),
        Event(severity="info", kind="qty_change", symbol="AAPL",
              message="AAPL qty changed: 3 → 4"),
    ]
    html = build_alert_email_html(events, account_equity="15000.00")
    assert "Portfolio Alert" in html
    assert "15,000.00" in html
    assert "Equity moved -3.20%" in html
    # Both severity tokens appear (alert in red, info in blue).
    assert "#fb7185" in html
    assert "#60a5fa" in html


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
