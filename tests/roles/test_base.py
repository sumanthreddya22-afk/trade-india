import datetime as dt
import pytest

from trading_bot.roles.base import (
    Role, RoleResult, ReportCard, Health, RoleStatus, HealthStatus,
)


def test_role_result_dataclass():
    started = dt.datetime(2026, 4, 28, 10, 0, 0, tzinfo=dt.timezone.utc)
    finished = dt.datetime(2026, 4, 28, 10, 0, 1, tzinfo=dt.timezone.utc)
    r = RoleResult(
        role_name="stock_scanner",
        started_at=started,
        finished_at=finished,
        status=RoleStatus.OK,
        latency_ms=1234,
        outputs={"placed": 1, "vetoed": 0},
    )
    assert r.role_name == "stock_scanner"
    assert r.status == RoleStatus.OK
    assert r.latency_ms == 1234
    assert r.outputs["placed"] == 1
    assert r.error_text is None


def test_role_result_with_error():
    started = dt.datetime.now(dt.timezone.utc)
    finished = dt.datetime.now(dt.timezone.utc)
    r = RoleResult(
        role_name="x", started_at=started, finished_at=finished,
        status=RoleStatus.ERROR, latency_ms=50, error_text="ValueError: bad",
    )
    assert r.status == RoleStatus.ERROR
    assert "ValueError" in r.error_text


def test_report_card_dataclass():
    card = ReportCard(
        role_name="stock_scanner",
        period_days=30,
        kpi_name="buy_win_rate_5d",
        kpi_value=0.62,
        summary="62% win rate on 18 buys; 7 losers / 11 winners",
        delta_vs_prior=0.04,
        health=HealthStatus.OK,
    )
    assert card.kpi_value == 0.62
    assert card.delta_vs_prior == 0.04


def test_health_dataclass():
    h = Health(status=HealthStatus.DEGRADED, detail="2 of last 5 runs errored")
    assert h.status == HealthStatus.DEGRADED
    assert "2 of last 5" in h.detail


def test_role_status_values():
    assert RoleStatus.OK.value == "ok"
    assert RoleStatus.ERROR.value == "error"
    assert RoleStatus.BLOCKED.value == "blocked"
    assert RoleStatus.HALTED.value == "halted"


def test_health_status_values():
    assert HealthStatus.OK.value == "OK"
    assert HealthStatus.DEGRADED.value == "DEGRADED"
    assert HealthStatus.BLOCKED.value == "BLOCKED"
    assert HealthStatus.FAIL.value == "FAIL"


def test_role_protocol_minimal_implementation():
    """A class implementing all Protocol attributes is recognized as a Role."""

    class FakeRole:
        name = "fake"
        tier = 1
        process = "daemon"
        job_description = "test only"
        sla_seconds = 30
        upstream_roles: list[str] = []
        downstream_roles: list[str] = []

        def run(self, ctx):
            return RoleResult(
                role_name="fake",
                started_at=dt.datetime.now(dt.timezone.utc),
                finished_at=dt.datetime.now(dt.timezone.utc),
                status=RoleStatus.OK,
                latency_ms=0,
            )

        def report_card(self, lookback_days):
            return ReportCard(
                role_name="fake", period_days=lookback_days,
                kpi_name="x", kpi_value=0.0, summary="ok",
            )

        def health_check(self):
            return Health(status=HealthStatus.OK)

    fake = FakeRole()
    assert isinstance(fake, Role)  # Protocol check works
