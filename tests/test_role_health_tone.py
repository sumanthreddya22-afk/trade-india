"""Role-health tone matrix tests.

Replaces the previous "rate-only" tone logic which painted any role with
<70% success rate red — even when the most recent run was healthy and the
"failure" was a single transient ConnectionResetError days ago on a small
sample (e.g. hold_spy_coordinator at 67% with N=3).

New rules:
  RED   — last run errored AND rate < 70
  AMBER — last run errored OR rate < 95
  GREEN — last run ok AND rate >= 95
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from trading_bot.lab_data import RoleHealthRow


def _render(rows: list[RoleHealthRow]) -> str:
    """Render the role_health fragment with the supplied rows."""
    templates_dir = (
        Path(__file__).resolve().parent.parent
        / "src" / "trading_bot" / "dashboard" / "templates"
    )
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    env.filters["et"] = lambda v, fmt="%b %d %I:%M %p ET": (
        v.strftime(fmt) if hasattr(v, "strftime") else ""
    )
    return env.get_template("_role_health.html").render(
        lab={"role_health": rows},
    )


def _row(*, name: str, rate: float, last_status: str = "ok",
         runs_30d: int = 100, runs_today: int = 5,
         last_error: str | None = None) -> RoleHealthRow:
    return RoleHealthRow(
        role_name=name,
        runs_today=runs_today,
        runs_30d=runs_30d,
        success_rate_pct=rate,
        last_run_at=dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc),
        last_status=last_status,
        last_error=last_error,
    )


def _classify_color(html: str, role_name: str) -> str:
    """Return 'green' | 'amber' | 'red' for the named role's row."""
    # Find the slice of HTML for this role.
    needle = f">{role_name}<"
    idx = html.find(needle)
    if idx == -1:
        raise AssertionError(f"role {role_name} not in html")
    # Look back ~600 chars to find the row's tone classes.
    window = html[max(0, idx - 600):idx]
    if "bg-emerald" in window:
        return "green"
    if "bg-rose" in window:
        return "red"
    if "bg-amber" in window:
        return "amber"
    raise AssertionError(f"no tone class found for {role_name}")


class TestToneMatrix:
    def test_high_sample_high_rate_last_ok_is_green(self):
        html = _render([_row(name="health_pulse", rate=100.0,
                             last_status="ok", runs_30d=2986)])
        assert _classify_color(html, "health_pulse") == "green"

    def test_low_sample_one_failure_long_ago_is_amber_not_red(self):
        """Regression: the operator hit this with hold_spy_coordinator —
        67% from 1/3 failures, but the latest run was OK."""
        html = _render([_row(name="hold_spy_coordinator", rate=67.0,
                             last_status="ok", runs_30d=3)])
        assert _classify_color(html, "hold_spy_coordinator") == "amber"

    def test_high_sample_with_persistent_failures_is_red(self):
        """backtest_engineer hit this — 23% over 130 runs, last status=error."""
        html = _render([
            _row(name="backtest_engineer", rate=23.0, last_status="error",
                 runs_30d=130, last_error="TypeError: Position.__init__() ..."),
        ])
        assert _classify_color(html, "backtest_engineer") == "red"

    def test_recent_transient_failure_high_overall_rate_is_amber(self):
        """stock_scanner: 93% (13/14), but most recent run was an error.
        Old logic made this amber via rate; new logic still amber via
        last_status=error rule."""
        html = _render([_row(name="stock_scanner", rate=93.0,
                             last_status="error", runs_30d=14,
                             last_error="ConnectionResetError")])
        assert _classify_color(html, "stock_scanner") == "amber"

    def test_low_sample_warning_marker_renders(self):
        html = _render([_row(name="calibrator", rate=100.0,
                             last_status="ok", runs_30d=1)])
        # The "⚠" appears next to "1 in 30d" because runs_30d < 10
        assert "⚠" in html

    def test_high_sample_no_warning_marker(self):
        html = _render([_row(name="health_pulse", rate=100.0,
                             last_status="ok", runs_30d=2986)])
        assert "⚠" not in html

    def test_tooltip_shows_last_error_and_timestamp(self):
        html = _render([_row(name="x", rate=23.0, last_status="error",
                             runs_30d=130, last_error="TypeError: bad")])
        assert "TypeError: bad" in html
