"""W1.4 — Orchestrator persists every decision to the DecisionStore.

Today, only `placed_order` decisions get written to trade_journal; rejections
and skips disappear. After W1.4, every decision branch (placed/rejected/
skipped/api_error) writes to the new `decisions` table with full audit
metadata. This makes "why didn't NVDA enter today?" answerable from a single
SQL query.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trading_bot.shared.alpaca_client import AccountSnapshot, OrderResult, Position
from trading_bot.decisions_store import DecisionStore
from trading_bot.orchestrator import ScanResult, TradeOrchestrator
from trading_bot.state import WatchlistEntry
from trading_bot.state_db import Base, get_engine
from trading_bot.strategy import Signal, SignalAction


# Fixtures — shamelessly copied from tests/test_orchestrator.py so this file
# can run standalone without pulling in fixtures from another module.

def _config():
    from trading_bot.shared.config import (
        AllocationConfig, AppConfig, EmailConfig, RegimeAllocation,
        RiskConfig, StorageConfig, StrategyConfig,
    )
    return AppConfig(
        risk=RiskConfig(
            daily_loss_limit_pct=2.0, weekly_loss_limit_pct=5.0,
            per_trade_risk_pct=1.0, max_position_pct=10.0,
            max_symbol_concentration_pct=5.0, max_consecutive_losing_days=3,
        ),
        allocation=AllocationConfig(
            stocks_max_pct=70.0, crypto_max_pct=30.0,
            options_max_pct=20.0, cash_floor_pct=10.0,
        ),
        regime_allocations={
            "trending_up": RegimeAllocation(stocks=60, crypto=25, options=15, cash=0),
        },
        email=EmailConfig(to="t@x.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
        strategy=StrategyConfig(
            earnings_gate_enabled=False, macro_shock_gate_enabled=False,
            crypto_fear_greed_enabled=False, crypto_reddit_spike_enabled=False,
            crypto_coingecko_enabled=False, insider_cluster_enabled=False,
        ),
    )


def _account():
    return AccountSnapshot(
        equity=Decimal("15000"), cash=Decimal("15000"),
        buying_power=Decimal("30000"), portfolio_value=Decimal("15000"),
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
def state_db(tmp_path: Path):
    db = tmp_path / "state.db"
    Base.metadata.create_all(get_engine(db))
    return db


@pytest.fixture
def store(state_db: Path):
    return DecisionStore(state_db)


@pytest.fixture
def watchlist():
    return [WatchlistEntry(symbol="AAPL", asset_class="stock", notes="")]


class TestPersistsDecisions:
    def test_skipped_existing_position_is_persisted(self, watchlist, store):
        market = MagicMock(); market.get_daily_bars.return_value = _bars()
        alpaca = MagicMock()
        alpaca.get_account.return_value = _account()
        alpaca.get_positions.return_value = [
            Position(
                symbol="AAPL", qty=Decimal("3"), market_value=Decimal("585"),
                avg_entry_price=Decimal("195"), current_price=Decimal("195"),
                unrealized_pl=Decimal("0"), asset_class="us_equity",
            )
        ]
        journal = MagicMock()

        orch = TradeOrchestrator(
            config=_config(), market_data=market, alpaca=alpaca, journal=journal,
            regime="trending_up", decision_store=store,
        )
        orch.scan(watchlist=watchlist)
        rows = store.recent(limit=10)
        assert len(rows) == 1
        assert rows[0].action == "skipped_existing_position"
        assert rows[0].symbol == "AAPL"
        # Audit metadata is populated
        import json
        audit = json.loads(rows[0].audit_json)
        assert audit["regime"] == "trending_up"
        assert audit["timestamp_utc"]
        assert audit["strategy_version"]

    def test_placed_order_is_persisted(self, watchlist, monkeypatch, store):
        market = MagicMock(); market.get_daily_bars.return_value = _bars()
        # Force a BUY signal regardless of the bars
        forced = Signal(
            symbol="AAPL", action=SignalAction.BUY,
            qty=Decimal("2"), entry_price=Decimal("195"),
            stop_loss_price=Decimal("190"), reason="test",
        )
        from trading_bot.strategy import MomentumStrategy
        monkeypatch.setattr(MomentumStrategy, "evaluate", lambda *a, **k: forced)

        alpaca = MagicMock()
        alpaca.get_account.return_value = _account()
        alpaca.get_positions.return_value = []
        alpaca.get_open_order_symbols.return_value = set()
        alpaca.place_order_with_stop_loss.return_value = OrderResult(
            entry_order_id="o-1", stop_loss_order_id="o-1-stop",
        )
        journal = MagicMock(); journal.traded_today.return_value = set()

        orch = TradeOrchestrator(
            config=_config(), market_data=market, alpaca=alpaca, journal=journal,
            regime="trending_up", decision_store=store,
        )
        orch.scan(watchlist=watchlist)
        rows = store.recent(limit=10)
        # Expect exactly one decision row for AAPL = placed_order
        placed = [r for r in rows if r.action == "placed_order"]
        assert len(placed) == 1
        assert placed[0].symbol == "AAPL"
        assert placed[0].entry_order_id == "o-1"
        assert placed[0].stop_loss_order_id == "o-1-stop"

    def test_rejected_by_risk_is_persisted(self, watchlist, monkeypatch, store):
        from trading_bot.shared.risk_manager import RiskManager
        from trading_bot.exceptions import RiskRuleViolation

        market = MagicMock(); market.get_daily_bars.return_value = _bars()
        forced = Signal(
            symbol="AAPL", action=SignalAction.BUY,
            qty=Decimal("2"), entry_price=Decimal("195"),
            stop_loss_price=Decimal("190"), reason="test",
        )
        from trading_bot.strategy import MomentumStrategy
        monkeypatch.setattr(MomentumStrategy, "evaluate", lambda *a, **k: forced)

        # Risk gate rejects every order
        def reject(*args, **kwargs):
            raise RiskRuleViolation(rule="too_concentrated", detail="hypothetical")
        monkeypatch.setattr(RiskManager, "check", reject)

        alpaca = MagicMock()
        alpaca.get_account.return_value = _account()
        alpaca.get_positions.return_value = []
        alpaca.get_open_order_symbols.return_value = set()
        journal = MagicMock(); journal.traded_today.return_value = set()

        orch = TradeOrchestrator(
            config=_config(), market_data=market, alpaca=alpaca, journal=journal,
            regime="trending_up", decision_store=store,
        )
        orch.scan(watchlist=watchlist)
        rows = store.recent(limit=10)
        rej = [r for r in rows if r.action == "rejected_by_risk"]
        assert len(rej) == 1
        assert "too_concentrated" in rej[0].reason

    def test_stale_bars_emit_skipped_stale_data(self, watchlist, store):
        """W2a — orchestrator runs the freshness gate before the strategy."""
        market = MagicMock()
        # Bars whose most recent timestamp is from 2026-01-01 — way > 48h old
        import pandas as pd
        old_idx = pd.date_range("2025-11-01", periods=40, freq="D", tz="UTC")
        market.get_daily_bars.return_value = pd.DataFrame(
            {"open": [100 + i for i in range(40)],
             "high": [101 + i for i in range(40)],
             "low": [99 + i for i in range(40)],
             "close": [100 + i for i in range(40)],
             "volume": [1_000_000] * 40},
            index=old_idx,
        )
        alpaca = MagicMock()
        alpaca.get_account.return_value = _account()
        alpaca.get_positions.return_value = []
        alpaca.get_open_order_symbols.return_value = set()
        journal = MagicMock(); journal.traded_today.return_value = set()

        orch = TradeOrchestrator(
            config=_config(), market_data=market, alpaca=alpaca, journal=journal,
            regime="trending_up", decision_store=store,
        )
        result = orch.scan(watchlist=watchlist)
        skipped = [d for d in result.decisions if d.action == "skipped_stale_data"]
        assert len(skipped) == 1
        assert skipped[0].data_quality.fresh is False
        # Persisted with truthful data_quality flags
        rows = store.recent(limit=10)
        assert any(r.action == "skipped_stale_data" for r in rows)
        alpaca.place_order_with_stop_loss.assert_not_called()

    def test_restricted_symbol_skipped_before_bar_fetch(self, watchlist, tmp_path, store):
        """W2b — a symbol on the restricted list never reaches the data fetch."""
        import yaml
        rl = tmp_path / "restricted.yaml"
        rl.write_text(yaml.safe_dump({"symbols": ["AAPL"]}))

        market = MagicMock()
        market.get_daily_bars.return_value = _bars()
        alpaca = MagicMock()
        alpaca.get_account.return_value = _account()
        alpaca.get_positions.return_value = []
        alpaca.get_open_order_symbols.return_value = set()
        journal = MagicMock(); journal.traded_today.return_value = set()

        orch = TradeOrchestrator(
            config=_config(), market_data=market, alpaca=alpaca, journal=journal,
            regime="trending_up", decision_store=store,
            restricted_list_path=rl,
        )
        orch.scan(watchlist=watchlist)
        # AAPL is the only symbol in the watchlist — should be skipped.
        market.get_daily_bars.assert_not_called()
        rows = store.recent(limit=10)
        assert any(r.action == "skipped_restricted" for r in rows)
        # Compliance flags persisted truthfully
        import json
        rec = next(r for r in rows if r.action == "skipped_restricted")
        compliance = json.loads(rec.compliance_json)
        assert compliance["restricted_list_clear"] is False

    def test_unapproved_venue_escalates_for_every_symbol(self, watchlist, store):
        """W2b — venue gate fails closed. The whole watchlist becomes
        escalate_to_human; no orders submitted."""
        market = MagicMock()
        alpaca = MagicMock()
        alpaca.get_account.return_value = _account()
        alpaca.get_positions.return_value = []
        alpaca.get_open_order_symbols.return_value = set()
        journal = MagicMock(); journal.traded_today.return_value = set()

        orch = TradeOrchestrator(
            config=_config(), market_data=market, alpaca=alpaca, journal=journal,
            regime="trending_up", decision_store=store,
            approved_venue_url="https://api.alpaca.markets",  # LIVE — not approved
        )
        result = orch.scan(watchlist=watchlist)
        assert all(d.action == "escalate_to_human" for d in result.decisions)
        alpaca.place_order_with_stop_loss.assert_not_called()

    def test_no_store_is_safe_legacy_path(self, watchlist):
        """Existing call sites that don't pass decision_store still work."""
        market = MagicMock(); market.get_daily_bars.return_value = _bars()
        alpaca = MagicMock()
        alpaca.get_account.return_value = _account()
        alpaca.get_positions.return_value = []
        alpaca.get_open_order_symbols.return_value = set()
        journal = MagicMock(); journal.traded_today.return_value = set()
        orch = TradeOrchestrator(
            config=_config(), market_data=market, alpaca=alpaca, journal=journal,
            regime="trending_up",  # decision_store omitted
        )
        result = orch.scan(watchlist=watchlist)
        # Just verifying no crash and decisions still come back in-memory
        assert isinstance(result, ScanResult)
