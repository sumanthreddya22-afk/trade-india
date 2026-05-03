import os

import pytest

from trading_bot.shared.alpaca_client import AlpacaClient
from trading_bot.shared.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Integration test — set RUN_INTEGRATION=1 to enable",
)


def test_real_paper_account_returns_account():
    settings = Settings()
    client = AlpacaClient(settings)
    account = client.get_account()
    assert account.equity > 0
    assert account.portfolio_value > 0


def test_real_paper_account_returns_positions():
    settings = Settings()
    client = AlpacaClient(settings)
    positions = client.get_positions()
    assert isinstance(positions, list)
