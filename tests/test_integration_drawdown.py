"""Integration test: drawdown breach writes pause.flag.

Task 22 — Phase 1 plan.

Does NOT spawn any subprocess — directly instantiates AccountSentinel with
an in-memory SQLite engine and a mock Alpaca client, then asserts that a
>20% drawdown from HWM causes pause.flag to be written.
"""
import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.state_db import Base
from trading_bot.state_hwm import update_hwm
from trading_bot.watchdog_account import AccountSentinel


@pytest.mark.integration
def test_drawdown_breach_writes_pause_flag(tmp_path):
    """End-to-end: HWM at 100k, current equity 78k → 22% DD → pause.flag."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)

        # Seed the HWM to $100k.
        with Session(engine) as s:
            update_hwm(s, account="paper", equity=100_000.0)

        # Fake Alpaca client returning equity at $78k (22% below HWM).
        alpaca = MagicMock()
        alpaca.get_account.return_value = MagicMock(equity=Decimal("78000"))

        sentinel = AccountSentinel(
            engine=engine,
            alpaca=alpaca,
            pause_flag_path=tmp_path / "pause.flag",
            max_dd_pct=20.0,
            account="paper",
        )
        verdict = sentinel.check()

        # Verdict should indicate a pause was triggered.
        assert verdict.paused is True, (
            f"Expected paused=True but got {verdict.paused}. "
            f"drawdown_pct={verdict.drawdown_pct:.2f}%"
        )

        # pause.flag must exist on disk.
        assert (tmp_path / "pause.flag").exists(), "pause.flag was not written to disk"

        # pause.flag must contain the word "drawdown" (set_pause embeds the reason).
        content = (tmp_path / "pause.flag").read_text().lower()
        assert "drawdown" in content, (
            f"Expected 'drawdown' in pause.flag content: {content!r}"
        )

        # The drawdown percentage should be correct (22%).
        assert verdict.drawdown_pct == pytest.approx(22.0, abs=0.01), (
            f"Expected ~22% drawdown, got {verdict.drawdown_pct:.4f}%"
        )

    finally:
        os.unlink(db_path)


@pytest.mark.integration
def test_no_pause_flag_when_drawdown_below_threshold(tmp_path):
    """When drawdown is 18% (below the 20% limit), no pause.flag is written."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)

        with Session(engine) as s:
            update_hwm(s, account="paper", equity=100_000.0)

        alpaca = MagicMock()
        alpaca.get_account.return_value = MagicMock(equity=Decimal("82000"))  # 18% down

        sentinel = AccountSentinel(
            engine=engine,
            alpaca=alpaca,
            pause_flag_path=tmp_path / "pause.flag",
            max_dd_pct=20.0,
            account="paper",
        )
        verdict = sentinel.check()

        assert verdict.paused is False
        assert not (tmp_path / "pause.flag").exists()
        assert 17.0 < verdict.drawdown_pct < 20.0

    finally:
        os.unlink(db_path)


@pytest.mark.integration
def test_hwm_advances_when_equity_hits_new_high(tmp_path):
    """After a new equity high, HWM advances and drawdown resets to 0."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)

        with Session(engine) as s:
            update_hwm(s, account="paper", equity=100_000.0)

        alpaca = MagicMock()
        alpaca.get_account.return_value = MagicMock(equity=Decimal("110000"))  # new high

        sentinel = AccountSentinel(
            engine=engine,
            alpaca=alpaca,
            pause_flag_path=tmp_path / "pause.flag",
            max_dd_pct=20.0,
            account="paper",
        )
        verdict = sentinel.check()

        assert verdict.paused is False
        assert verdict.drawdown_pct == 0.0
        assert verdict.hwm == pytest.approx(110_000.0)
        assert not (tmp_path / "pause.flag").exists()

    finally:
        os.unlink(db_path)
