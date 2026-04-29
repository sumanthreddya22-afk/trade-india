from decimal import Decimal
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def test_bot_status_runs_and_calls_email():
    from trading_bot.cli import main

    fake_account = MagicMock(equity=Decimal("100000"), cash=Decimal("50000"))
    fake_positions = []

    with patch("trading_bot.cli.AlpacaClient") as MockClient, patch(
        "trading_bot.cli.EmailSender"
    ) as MockEmail, patch("trading_bot.cli.Settings") as MockSettings, patch(
        "trading_bot.cli.load_config"
    ) as MockCfg:
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k",
            alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u@x.com",
            gmail_app_password="p",
            bot_mode="paper",
        )
        MockCfg.return_value = MagicMock(email=MagicMock(to="u@x.com"))
        MockClient.return_value.get_account.return_value = fake_account
        MockClient.return_value.get_positions.return_value = fake_positions
        sender = MockEmail.return_value

        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        sender.send.assert_called_once()
        kwargs = sender.send.call_args.kwargs
        assert "Status" in kwargs["subject"]
        # Equity is formatted with thousands separator in the polished email shell.
        assert "100,000" in kwargs["html_body"]


def test_bot_dry_run_passes_risk_manager():
    from trading_bot.cli import main

    fake_account = MagicMock(
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        portfolio_value=Decimal("100000"),
    )
    with patch("trading_bot.cli.AlpacaClient") as MockClient, patch(
        "trading_bot.cli.Settings"
    ) as MockSettings, patch("trading_bot.cli.load_config") as MockCfg, patch(
        "trading_bot.cli._build_risk_state"
    ) as MockState:
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k",
            alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u@x.com",
            gmail_app_password="p",
            bot_mode="paper",
        )
        MockCfg.return_value = _real_config_for_test()
        MockClient.return_value.get_account.return_value = fake_account
        MockClient.return_value.get_positions.return_value = []
        MockState.return_value = _real_state_zero()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dry-run",
                "--symbol", "AAPL",
                "--side", "buy",
                "--qty", "10",
                "--price", "195.00",
                "--stop", "192.00",
                "--regime", "trending_up",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "PASS" in result.output


def test_bot_dry_run_reports_violation():
    from trading_bot.cli import main

    fake_account = MagicMock(
        equity=Decimal("100000"),
        cash=Decimal("50000"),
        buying_power=Decimal("100000"),
        portfolio_value=Decimal("100000"),
    )
    with patch("trading_bot.cli.AlpacaClient") as MockClient, patch(
        "trading_bot.cli.Settings"
    ) as MockSettings, patch("trading_bot.cli.load_config") as MockCfg, patch(
        "trading_bot.cli._build_risk_state"
    ) as MockState:
        MockSettings.return_value = MagicMock(
            alpaca_api_key="k",
            alpaca_api_secret="s",
            alpaca_base_url="https://paper-api.alpaca.markets/v2",
            gmail_user="u@x.com",
            gmail_app_password="p",
            bot_mode="paper",
        )
        MockCfg.return_value = _real_config_for_test()
        MockClient.return_value.get_account.return_value = fake_account
        MockClient.return_value.get_positions.return_value = []
        MockState.return_value = _real_state_zero()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "dry-run",
                "--symbol", "AAPL",
                "--side", "buy",
                "--qty", "100",  # oversized
                "--price", "195.00",
                "--stop", "192.00",
                "--regime", "trending_up",
            ],
        )
        assert result.exit_code != 0
        assert "max_position_pct" in result.output


def _real_config_for_test():
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
        email=EmailConfig(to="u@x.com", daily_summary_time_et="16:30", weekly_summary_day="Sunday"),
        storage=StorageConfig(trade_journal_path="data/test.db"),
    )


def _real_state_zero():
    from trading_bot.risk_manager import RiskState
    return RiskState(
        daily_pnl_pct=Decimal("0"),
        weekly_pnl_pct=Decimal("0"),
        consecutive_losing_days=0,
        halted=False,
    )


def test_screen_universe_writes_snapshot(tmp_path, monkeypatch):
    from trading_bot.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategy").mkdir()

    with patch("trading_bot.cli.build_universe_from_seed_list") as mock_build, \
         patch("trading_bot.cli.AlpacaClient"), \
         patch("trading_bot.cli.MarketDataClient"), \
         patch("trading_bot.cli.Settings"), \
         patch("trading_bot.cli.load_config"):
        from decimal import Decimal
        from trading_bot.universe import LiquidAsset
        mock_build.return_value = [
            LiquidAsset(symbol="NVDA", name="NVIDIA",
                        asset_class="us_equity", exchange="NASDAQ",
                        last_price=Decimal("450"), avg_dollar_volume=Decimal("8e9"),
                        fractionable=True, sector_tags=("ai", "semiconductors")),
        ]

        runner = CliRunner()
        result = runner.invoke(main, ["screen-universe"])

    assert result.exit_code == 0, result.output
    snapshot = (tmp_path / "strategy" / "latest_intelligence.md").read_text()
    assert "NVDA" in snapshot


def test_load_active_universe_falls_back_to_watchlist(tmp_path, monkeypatch):
    """Phase 0a: with no opportunities.md, returns watchlist.yaml verbatim."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategy").mkdir()
    (tmp_path / "strategy" / "watchlist.yaml").write_text(
        "symbols:\n"
        "  - symbol: AAPL\n    asset_class: stock\n    notes: x\n"
        "  - symbol: BTC/USD\n    asset_class: crypto\n    notes: y\n"
    )
    from trading_bot.cli import _load_active_universe

    universe = _load_active_universe()
    syms = [e.symbol for e in universe]
    assert syms == ["AAPL", "BTC/USD"]


def test_load_active_universe_merges_opportunities_with_crypto(tmp_path, monkeypatch):
    """Phase 0a: top stocks come from opportunities.md; crypto from watchlist.yaml."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategy").mkdir()
    (tmp_path / "strategy" / "watchlist.yaml").write_text(
        "symbols:\n"
        "  - symbol: SPY\n    asset_class: stock\n    notes: legacy\n"
        "  - symbol: BTC/USD\n    asset_class: crypto\n    notes: btc\n"
        "  - symbol: ETH/USD\n    asset_class: crypto\n    notes: eth\n"
    )
    (tmp_path / "strategy" / "opportunities.md").write_text(
        "# Opportunities\n"
        "### 1. NVDA (us_equity)\n"
        "### 2. AMD (us_equity)\n"
        "### 3. TSLA (us_equity)\n"
    )
    from trading_bot.cli import _load_active_universe

    universe = _load_active_universe()
    syms = [e.symbol for e in universe]
    # Top-ranked stocks first, then crypto from watchlist
    assert syms[:3] == ["NVDA", "AMD", "TSLA"]
    assert "BTC/USD" in syms
    assert "ETH/USD" in syms
    # Legacy SPY (in watchlist but not in opportunities) is dropped
    assert "SPY" not in syms


def test_rank_writes_opportunities(tmp_path, monkeypatch):
    from trading_bot.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategy").mkdir()

    from decimal import Decimal
    from trading_bot.universe import LiquidAsset

    with patch("trading_bot.cli.build_universe_from_seed_list") as mock_build, \
         patch("trading_bot.cli.AlpacaClient"), \
         patch("trading_bot.cli.MarketDataClient") as mock_md, \
         patch("trading_bot.cli.Settings"), \
         patch("trading_bot.cli.load_config"):
        mock_build.return_value = [
            LiquidAsset(symbol="AAA", name="AAA", asset_class="us_equity",
                        exchange="NASDAQ", last_price=Decimal("100"),
                        avg_dollar_volume=Decimal("1e9"), fractionable=True,
                        sector_tags=("ai",)),
        ]
        import pandas as pd
        def fake_bars(symbol, lookback_days=60):
            closes = [100 + i * 0.5 for i in range(60)]
            return pd.DataFrame({"close": closes, "volume": [1e6] * 60})
        mock_md.return_value.get_daily_bars.side_effect = fake_bars

        runner = CliRunner()
        result = runner.invoke(main, ["rank"])

    assert result.exit_code == 0, result.output
    text = (tmp_path / "strategy" / "opportunities.md").read_text()
    assert "AAA" in text
