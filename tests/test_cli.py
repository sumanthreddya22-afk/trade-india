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
        assert "100000" in kwargs["html_body"]


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
