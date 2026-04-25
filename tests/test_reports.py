from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.alpaca_client import AccountSnapshot, Position
from trading_bot.orchestrator import Decision, ScanResult
from trading_bot.reports import build_daily_report_html


def test_daily_report_contains_account_and_decisions():
    account = AccountSnapshot(
        equity=Decimal("15123.45"),
        cash=Decimal("12000"),
        buying_power=Decimal("24000"),
        portfolio_value=Decimal("15123.45"),
    )
    positions = [
        Position(
            symbol="AAPL",
            qty=Decimal("3"),
            market_value=Decimal("585"),
            avg_entry_price=Decimal("195"),
            unrealized_pl=Decimal("12.50"),
            asset_class="us_equity",
        )
    ]
    scan = ScanResult(
        decisions=[
            Decision(symbol="MSFT", action="placed_order",
                     reason="rsi=58.0 macd>0.020 close>EMA20",
                     entry_order_id="e-1", stop_loss_order_id="s-1"),
            Decision(symbol="QQQ", action="hold", reason="rsi 45.2 outside [55, 70]"),
            Decision(symbol="SPY", action="skipped_existing_position"),
        ],
        timestamp=datetime(2026, 4, 25, 20, 30, tzinfo=timezone.utc),
    )

    html = build_daily_report_html(
        account=account, positions=positions, scan=scan,
        spy_daily_change_pct=Decimal("1.20"),
        regime="trending_up",
    )
    assert "15123.45" in html
    assert "AAPL" in html
    assert "MSFT" in html
    assert "placed_order" in html
    assert "trending_up" in html
    assert "1.20" in html
