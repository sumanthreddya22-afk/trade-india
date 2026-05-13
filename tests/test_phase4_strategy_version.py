"""Phase 4 — strategy_version writer + reader + expiry."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.registry import (
    StrategyVersion, VersionNotFound,
    get_active_version, list_versions, register_version,
)


def _common(**kw):
    defaults = dict(
        strategy_id="ETF_MOM", strategy_ver=1,
        code_hash="c", config_hash="cf",
        thesis_id="edge_thesis_v1", hypothesis_id="edge_thesis_v1",
        lane="etf_momentum", owner="op",
    )
    defaults.update(kw)
    return defaults


def test_register_research_only_no_artifact_required(ledger_conn) -> None:
    v = register_version(ledger_conn, **_common())
    assert v.status == "research_only"
    assert v.validation_artifact_id is None


def test_register_active_status_requires_artifact(ledger_conn) -> None:
    with pytest.raises(ValueError, match=r"validation_artifact_id"):
        register_version(ledger_conn,
                         **_common(status="tiny_paper"))


def test_register_active_with_artifact_ok(ledger_conn) -> None:
    v = register_version(ledger_conn,
                         **_common(status="tiny_paper",
                                   validation_artifact_id="art-1",
                                   expiry_date=dt.date(2026, 8, 13)))
    assert v.status == "tiny_paper"
    assert v.is_active_for_trading(now=dt.datetime(
        2026, 5, 13, tzinfo=dt.timezone.utc))


def test_expired_version_not_active(ledger_conn) -> None:
    v = register_version(ledger_conn,
                         **_common(status="tiny_paper",
                                   validation_artifact_id="art-1",
                                   expiry_date=dt.date(2026, 5, 12)))
    assert not v.is_active_for_trading(now=dt.datetime(
        2026, 5, 13, tzinfo=dt.timezone.utc))


def test_get_active_returns_latest_ver(ledger_conn) -> None:
    register_version(ledger_conn, **_common(strategy_ver=1))
    register_version(ledger_conn, **_common(strategy_ver=2))
    v = get_active_version(ledger_conn, "ETF_MOM")
    assert v.strategy_ver == 2


def test_version_not_found(ledger_conn) -> None:
    with pytest.raises(VersionNotFound):
        get_active_version(ledger_conn, "GHOST")


def test_list_versions_ordered(ledger_conn) -> None:
    register_version(ledger_conn, **_common(strategy_ver=2))
    register_version(ledger_conn, **_common(strategy_ver=1))
    versions = list_versions(ledger_conn, "ETF_MOM")
    assert [v.strategy_ver for v in versions] == [1, 2]
