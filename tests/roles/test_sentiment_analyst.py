# tests/roles/test_sentiment_analyst.py
import os, tempfile
from unittest.mock import patch, MagicMock
import pytest
from sqlalchemy import create_engine
from trading_bot.state_db import Base
from trading_bot.roles.base import RoleStatus
from trading_bot.roles.sentiment_analyst import SentimentAnalystRole


@pytest.fixture
def engine():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    os.unlink(path)


def test_charter():
    role = SentimentAnalystRole(engine=None)
    assert role.name == "sentiment_analyst"
    assert role.tier == 1


def test_do_work_invokes_news_warm(engine):
    role = SentimentAnalystRole(engine=engine)
    with patch("trading_bot.cli.news_warm") as mock_cmd:
        mock_cmd.callback = MagicMock(return_value=None)
        result = role.safe_run(ctx={})
        mock_cmd.callback.assert_called_once()
    assert result.status == RoleStatus.OK


def test_do_work_handles_exception(engine):
    role = SentimentAnalystRole(engine=engine)
    with patch("trading_bot.cli.news_warm") as mock_cmd:
        mock_cmd.callback.side_effect = ConnectionError("polygon down")
        result = role.safe_run(ctx={})
    assert result.status == RoleStatus.ERROR


def test_kpi_default(engine):
    role = SentimentAnalystRole(engine=engine)
    name, _, _ = role._kpi_value(lookback_days=30)
    assert name == "floor_block_post_5d_return"
