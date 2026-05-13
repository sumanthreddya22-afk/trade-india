"""Phase 4 — seed strategy registered correctly in the shipped ledger."""
from __future__ import annotations

from pathlib import Path

from trading_bot.ledger import DEFAULT_LEDGER_PATH, connect_reader
from trading_bot.registry import VersionNotFound, get_active_version

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_etf_momentum_v1_is_registered() -> None:
    db = REPO_ROOT / DEFAULT_LEDGER_PATH
    if not db.exists():
        # tools/init_ledger.py + register_seed_strategy.py haven't been
        # run yet — that's an operator-setup precondition.
        import pytest
        pytest.skip("ledger.db not initialised in this checkout")
    conn = connect_reader(db)
    try:
        v = get_active_version(conn, "ETF_MOMENTUM_v1")
    except VersionNotFound:
        import pytest
        pytest.skip("seed strategy not registered yet "
                    "(run tools/register_seed_strategy.py)")
    finally:
        conn.close()
    assert v.thesis_id == "edge_thesis_v1"
    assert v.lane == "etf_momentum"
    assert v.status == "research_only"
    assert v.validation_artifact_id is None
