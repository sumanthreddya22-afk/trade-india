from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from trading_bot.dashboard.data import (
    DashboardSnapshot,
    KpiBlock,
    StatsBlock,
    _build_stats,
)
from trading_bot.reconciliation import ClosedTrade


def _trade(symbol: str, pnl: float, exit_offset_h: int) -> ClosedTrade:
    base = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    return ClosedTrade(
        symbol=symbol, side="buy",
        qty=Decimal("10"),
        entry_price=Decimal("100"),
        exit_price=Decimal(str(100 + pnl / 10)),
        realized_pnl=Decimal(str(pnl)),
        pnl_pct=pnl / 100,
        strategy="momentum", regime="trending_up",
        entry_time=base, exit_time=base.replace(hour=12 + exit_offset_h),
        hold_hours=float(exit_offset_h),
        entry_order_id=f"ord-{symbol}-{exit_offset_h}",
    )


def test_wheel_fragment_route_returns_html():
    """Phase 5: /fragment/wheel renders even with no open cycles."""
    from fastapi.testclient import TestClient
    from trading_bot.dashboard.app import app
    client = TestClient(app)
    r = client.get("/fragment/wheel")
    assert r.status_code == 200
    assert "Wheel" in r.text


def test_stats_empty_journal():
    s = _build_stats([])
    assert s.total_trades == 0
    assert s.win_rate_pct is None
    assert s.profit_factor is None
    assert s.streak == "—"


def test_stats_basic_metrics():
    closed = [
        _trade("AAA", +50.0, 1),  # win
        _trade("BBB", -20.0, 2),  # loss
        _trade("CCC", +30.0, 3),  # win
        _trade("DDD", +40.0, 4),  # win
    ]
    s = _build_stats(closed)
    assert s.total_trades == 4
    assert s.wins == 3 and s.losses == 1
    assert s.win_rate_pct == 75.0
    assert s.profit_factor == round(120 / 20, 2)
    assert s.best_trade_symbol == "AAA"
    assert s.worst_trade_symbol == "BBB"
    assert s.streak == "2W"  # last two are wins


def test_stats_loss_streak():
    closed = [
        _trade("AAA", +10.0, 1),
        _trade("BBB", -5.0, 2),
        _trade("CCC", -7.0, 3),
    ]
    s = _build_stats(closed)
    assert s.streak == "2L"


def test_dashboard_index_renders(tmp_path, monkeypatch):
    """Smoke test: the index page renders end-to-end without raising,
    even when every external data source fails."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategy").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "strategy" / "config.yaml").write_text(
        "risk: {daily_loss_limit_pct: 2, weekly_loss_limit_pct: 5, "
        "per_trade_risk_pct: 1, max_position_pct: 10, "
        "max_symbol_concentration_pct: 5, max_consecutive_losing_days: 3}\n"
        "allocation: {stocks_max_pct: 70, crypto_max_pct: 30, "
        "options_max_pct: 20, cash_floor_pct: 10}\n"
        "regime_allocations:\n"
        "  trending_up: {stocks: 60, crypto: 25, options: 15, cash: 0}\n"
        "  trending_down: {stocks: 30, crypto: 15, options: 10, cash: 45}\n"
        "  sideways: {stocks: 40, crypto: 20, options: 20, cash: 20}\n"
        "  risk_off: {stocks: 10, crypto: 5, options: 0, cash: 85}\n"
        "email: {to: u@x.com, daily_summary_time_et: '16:30', weekly_summary_day: 'Sunday'}\n"
        "storage: {trade_journal_path: data/test.db}\n"
        "regime: {vol_threshold_pct: 22.0}\n"
    )
    (tmp_path / "strategy" / "watchlist.yaml").write_text(
        "symbols:\n  - symbol: SPY\n    asset_class: stock\n    notes: x\n"
    )

    fake_settings = MagicMock(
        alpaca_api_key="k", alpaca_api_secret="s",
        alpaca_base_url="https://paper-api.alpaca.markets/v2",
        gmail_user="u@x.com", gmail_app_password="p", bot_mode="paper",
    )

    with patch("trading_bot.dashboard.app.Settings", return_value=fake_settings), \
         patch("trading_bot.dashboard.data.AlpacaClient") as MockAlpaca, \
         patch("trading_bot.dashboard.data.MarketDataClient"), \
         patch("trading_bot.dashboard.data.detect_regime") as MockRegime, \
         patch("trading_bot.dashboard.data.get_macro_snapshot") as MockMacro, \
         patch("trading_bot.dashboard.data.TradingClient") as MockTrading:
        from trading_bot.regime import Regime, RegimeReading
        MockRegime.return_value = RegimeReading(
            regime=Regime.TRENDING_UP, spy_close=500, ema_50=490, ema_200=470,
            vol_annualized_pct=14.5, confidence="high", notes="calm", vix=18.0,
        )
        MockMacro.return_value = MagicMock(vix=18.0)
        MockAlpaca.return_value.get_account.return_value = MagicMock(
            equity=Decimal("15000"), cash=Decimal("14000")
        )
        MockAlpaca.return_value.get_positions.return_value = []
        MockTrading.return_value.get_portfolio_history.return_value = MagicMock(
            equity=[14900, 14950, 15000], timestamp=[1714000000, 1714086400, 1714172800]
        )
        MockTrading.return_value.get_orders.return_value = []

        from trading_bot.dashboard.app import create_app
        app = create_app()
        client = TestClient(app)

        r = client.get("/")
        assert r.status_code == 200, r.text
        assert "Today's Trading Command Center" in r.text
        assert "trending up" in r.text.lower()
        assert "$15,000.00" in r.text  # equity rendered

        r = client.get("/api/snapshot")
        assert r.status_code == 200
        data = r.json()
        assert data["regime"] == "trending_up"
        assert data["kpi"]["equity"] == 15000.0
