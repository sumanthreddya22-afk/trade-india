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
