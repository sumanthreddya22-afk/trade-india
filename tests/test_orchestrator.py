from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trading_bot.alpaca_client import (
    AccountSnapshot,
    OrderResult,
    Position,
)
from trading_bot.orchestrator import ScanResult, TradeOrchestrator
from trading_bot.state import WatchlistEntry
from trading_bot.strategy import Signal, SignalAction


def _config():
    from trading_bot.config import (
        AllocationConfig,
        AppConfig,
        EmailConfig,
        RegimeAllocation,
        RiskConfig,
        StorageConfig,
    )
    return AppConfig(
        risk=RiskConfig(
            daily_loss_limit_pct=2.0,
            weekly_loss_limit_pct=5.0,
            per_trade_risk_pct=1.0,
            max_position_pct=10.0,
            max_symbol_concentration_pct=5.0,
            max_consecutive_losing_days=3,
        ),
        allocation=AllocationConfig(
            stocks_max_pct=70.0, crypto_max_pct=30.0, options_max_pct=20.0, cash_floor_pct=10.0
        ),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
            "trending_down": RegimeAllocation(stocks=30, crypto=15, options=10, cash=45),
            "sideways": RegimeAllocation(stocks=40, crypto=20, options=20, cash=20),
            "risk_off": RegimeAllocation(stocks=10, crypto=5, options=0, cash=85),
        },
        email=EmailConfig(to="t@x.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("15000"),
        cash=Decimal("15000"),
        buying_power=Decimal("30000"),
        portfolio_value=Decimal("15000"),
    )


def _bars():
    return pd.DataFrame(
        {"close": [100 + i for i in range(40)],
         "open": [100 + i for i in range(40)],
         "high": [101 + i for i in range(40)],
         "low": [99 + i for i in range(40)],
         "volume": [1_000_000] * 40},
        index=pd.date_range("2026-04-01", periods=40, freq="D", tz="UTC"),
    )


@pytest.fixture
def watchlist():
    return [
        WatchlistEntry(symbol="AAPL", asset_class="stock", notes=""),
        WatchlistEntry(symbol="MSFT", asset_class="stock", notes=""),
    ]


def test_orchestrator_skips_existing_positions(watchlist):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = [
        Position(
            symbol="AAPL", qty=Decimal("3"), market_value=Decimal("585"),
            avg_entry_price=Decimal("195"), unrealized_pl=Decimal("0"), asset_class="us_equity",
        )
    ]
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    result = orch.scan(watchlist=watchlist)
    assert isinstance(result, ScanResult)
    skipped_aapl = [d for d in result.decisions if d.symbol == "AAPL"][0]
    assert skipped_aapl.action == "skipped_existing_position"
    alpaca.place_order_with_stop_loss.assert_not_called()


def test_orchestrator_places_order_on_buy_signal(watchlist, monkeypatch):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()

    forced = Signal(
        symbol="MSFT",
        action=SignalAction.BUY,
        qty=Decimal("2"),
        entry_price=Decimal("139"),
        stop_loss_price=Decimal("133"),
        reason="forced",
    )

    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    alpaca.place_order_with_stop_loss.return_value = OrderResult(
        entry_order_id="e-1", stop_loss_order_id="s-1"
    )
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity: forced if sym == "MSFT" else
        Signal(sym, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"), "x"),
    )

    result = orch.scan(watchlist=watchlist)
    placed = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert placed.action == "placed_order"
    assert placed.entry_order_id == "e-1"
    alpaca.place_order_with_stop_loss.assert_called_once()
    journal.append.assert_called_once()


def test_orchestrator_skips_on_risk_violation(watchlist, monkeypatch):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars()

    forced = Signal(
        symbol="MSFT",
        action=SignalAction.BUY,
        qty=Decimal("100"),
        entry_price=Decimal("139"),
        stop_loss_price=Decimal("100"),
        reason="forced bad",
    )

    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    monkeypatch.setattr(
        orch._strategy, "evaluate",
        lambda sym, ind, equity: forced if sym == "MSFT" else
        Signal(sym, SignalAction.HOLD, Decimal("0"), Decimal("0"), Decimal("0"), "x"),
    )

    result = orch.scan(watchlist=watchlist)
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.action == "rejected_by_risk"
    assert "per_trade_risk_pct" in msft.reason or "max_position_pct" in msft.reason
    alpaca.place_order_with_stop_loss.assert_not_called()


def test_orchestrator_skips_when_bars_too_short(watchlist):
    market = MagicMock()
    market.get_daily_bars.return_value = _bars().head(5)
    alpaca = MagicMock()
    alpaca.get_account.return_value = _account()
    alpaca.get_positions.return_value = []
    journal = MagicMock()
    cfg = _config()

    orch = TradeOrchestrator(
        config=cfg, market_data=market, alpaca=alpaca, journal=journal, regime="trending_up"
    )
    result = orch.scan(watchlist=watchlist)
    for d in result.decisions:
        assert d.action == "skipped_insufficient_data"


from pathlib import Path

from trading_bot.orchestrator import load_ranked_watchlist


def test_load_ranked_watchlist_reads_opportunities(tmp_path: Path):
    md = tmp_path / "opportunities.md"
    md.write_text(
        "# Opportunities (Stage-2)\n\n"
        "## Ranked Candidates\n\n"
        "### 1. NVDA (us_equity)\n\n"
        "- Lanes: momentum\n"
        "- Conviction: 0.75\n\n"
        "### 2. BTC/USD (crypto)\n\n"
        "- Lanes: breakout\n"
        "- Conviction: 0.60\n"
    )
    entries = load_ranked_watchlist(md)
    syms = [e.symbol for e in entries]
    assert syms == ["NVDA", "BTC/USD"]
    assert entries[0].asset_class == "us_equity"
    assert entries[1].asset_class == "crypto"
