"""Lab Evolution view exposes last_productive_run separately so a 0-trial
no-op doesn't make the card look broken.

Background: param_search runs nightly at 02:00 ET. Some nights nothing has
drifted; the runner finishes status=ok with n_trials=0. Before this fix the
dashboard headlined "0 trials / —" even though yesterday's productive run
had 30 trials and a fitness of 3.96.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from trading_bot.lab_data import lab_evolution
from trading_bot.state_db import Base, EvolutionRun, get_engine


@pytest.fixture
def session(tmp_path: Path):
    db = tmp_path / "state.db"
    Base.metadata.create_all(get_engine(db))
    s = Session(get_engine(db))
    yield s
    s.close()


def _make_run(s: Session, *, started_at, n_trials, best_fitness, promoted=False):
    s.add(EvolutionRun(
        started_at=started_at, finished_at=started_at + dt.timedelta(minutes=2),
        template_name="momentum", n_trials=n_trials,
        best_fitness=best_fitness, best_params_hash="x",
        auto_promoted=1 if promoted else 0, promotion_gate_pass="ok",
    ))
    s.commit()


class TestProductiveRunSplit:
    def test_latest_run_productive_both_match(self, session):
        _make_run(
            session,
            started_at=dt.datetime(2026, 4, 28, 6, 0, tzinfo=dt.timezone.utc),
            n_trials=30, best_fitness=3.96, promoted=True,
        )
        ev = lab_evolution(session)
        assert ev.last_run_n_trials == 30
        assert ev.last_productive_run_n_trials == 30
        assert ev.last_productive_run_best_fitness == 3.96
        assert ev.last_run_promoted is True
        assert ev.last_productive_run_promoted is True

    def test_zero_trial_run_falls_back_to_yesterday(self, session):
        # Yesterday: productive
        _make_run(
            session,
            started_at=dt.datetime(2026, 4, 28, 6, 0, tzinfo=dt.timezone.utc),
            n_trials=30, best_fitness=3.96, promoted=False,
        )
        # Today: no-op
        _make_run(
            session,
            started_at=dt.datetime(2026, 4, 29, 6, 0, tzinfo=dt.timezone.utc),
            n_trials=0, best_fitness=None,
        )
        ev = lab_evolution(session)
        # Latest run reflects today (no-op)
        assert ev.last_run_n_trials == 0
        assert ev.last_run_best_fitness is None
        # Productive run still points at yesterday — the headline-worthy data
        assert ev.last_productive_run_n_trials == 30
        assert ev.last_productive_run_best_fitness == 3.96
        assert ev.last_productive_run_started_at == dt.datetime(
            2026, 4, 28, 6, 0, tzinfo=dt.timezone.utc,
        ) or ev.last_productive_run_started_at.replace(
            tzinfo=dt.timezone.utc
        ) == dt.datetime(2026, 4, 28, 6, 0, tzinfo=dt.timezone.utc)

    def test_no_runs_returns_empty_block(self, session):
        ev = lab_evolution(session)
        assert ev.last_run_started_at is None
        assert ev.last_productive_run_started_at is None
        assert ev.last_productive_run_n_trials == 0
        assert ev.top_leaderboard == []

    def test_only_zero_trial_runs_leaves_productive_empty(self, session):
        # Two consecutive no-op nights — never had a productive run.
        for d in (28, 29):
            _make_run(
                session,
                started_at=dt.datetime(2026, 4, d, 6, 0, tzinfo=dt.timezone.utc),
                n_trials=0, best_fitness=None,
            )
        ev = lab_evolution(session)
        assert ev.last_run_n_trials == 0
        assert ev.last_productive_run_started_at is None
        assert ev.last_productive_run_n_trials == 0
        assert ev.last_productive_run_best_fitness is None


class TestTemplateRendering:
    """The lab_evolution fragment must render the no-op warning banner when
    today's run is unproductive AND show the productive run as the headline."""

    def test_zero_trial_today_shows_warning_and_productive_headline(self, session, tmp_path: Path):
        _make_run(
            session,
            started_at=dt.datetime(2026, 4, 28, 6, 0, tzinfo=dt.timezone.utc),
            n_trials=30, best_fitness=3.96, promoted=False,
        )
        _make_run(
            session,
            started_at=dt.datetime(2026, 4, 29, 6, 0, tzinfo=dt.timezone.utc),
            n_trials=0, best_fitness=None,
        )
        # Render the template against this view
        from jinja2 import Environment, FileSystemLoader
        from datetime import datetime as _dt, timezone as _tz
        from zoneinfo import ZoneInfo

        templates_dir = (
            Path(__file__).resolve().parent.parent
            / "src" / "trading_bot" / "dashboard" / "templates"
        )
        env = Environment(loader=FileSystemLoader(str(templates_dir)))
        # _et filter mirroring the app
        def _et(value, fmt: str = "%b %d %-I:%M %p ET"):
            if value is None:
                return ""
            if not isinstance(value, _dt):
                return value
            v = value if value.tzinfo else value.replace(tzinfo=_tz.utc)
            return v.astimezone(ZoneInfo("America/New_York")).strftime(fmt)
        env.filters["et"] = _et

        ev = lab_evolution(session)
        html = env.get_template("_lab_evolution.html").render(
            lab={"lab_evolution": ev},
        )
        assert "produced 0 new trials" in html  # warning banner
        assert "Last Productive Search" in html
        # Headline numbers come from yesterday's productive run
        assert "30 trials" in html
        assert "3.96" in html
