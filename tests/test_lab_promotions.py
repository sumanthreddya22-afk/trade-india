import datetime as dt
import json
from pathlib import Path

import pytest


@pytest.fixture
def state_db(tmp_path):
    db_path = tmp_path / "state.db"
    from sqlalchemy import create_engine, text
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE lab_promotions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "promoted_at TIMESTAMP NOT NULL, "
            "version TEXT NOT NULL UNIQUE, "
            "template TEXT NOT NULL, "
            "git_sha TEXT NOT NULL, "
            "fitness_at_promotion REAL NOT NULL, "
            "params_json TEXT NOT NULL, "
            "risk_caps_json TEXT NOT NULL, "
            "scans_since_promote INTEGER NOT NULL DEFAULT 0, "
            "entries_since_promote INTEGER NOT NULL DEFAULT 0, "
            "near_misses_since_promote INTEGER NOT NULL DEFAULT 0, "
            "validated_at TIMESTAMP)"
        ))
    return db_path


def test_record_promotion_inserts(state_db):
    from trading_bot.lab_promotions import LabPromotionStore

    store = LabPromotionStore(state_db)
    store.record(
        promoted_at=dt.datetime(2026, 4, 28, 10, 1, tzinfo=dt.timezone.utc),
        version="auto-20260428-100154",
        template="momentum",
        git_sha="phase1-bootstrap",
        fitness=3.967,
        params={"rsi_lower": 50.0, "stop_pct": 6.11},
        risk_caps={"daily_loss_pct": 3.0, "max_position_pct": 10.0},
    )

    pending = store.pending_validation(now=dt.datetime(2026, 4, 28, 22, 0,
                                                       tzinfo=dt.timezone.utc))
    assert len(pending) == 1
    p = pending[0]
    assert p["version"] == "auto-20260428-100154"
    assert p["fitness_at_promotion"] == 3.967


def test_pending_validation_excludes_old(state_db):
    """Promotions older than 24h aren't pending anymore."""
    from trading_bot.lab_promotions import LabPromotionStore

    store = LabPromotionStore(state_db)
    promoted = dt.datetime(2026, 4, 26, 10, 0, tzinfo=dt.timezone.utc)
    store.record(
        promoted_at=promoted, version="v-old", template="momentum",
        git_sha="x", fitness=2.0,
        params={}, risk_caps={},
    )

    # 48h later — past the 24h validation window
    pending = store.pending_validation(now=dt.datetime(2026, 4, 28, 10, 0,
                                                       tzinfo=dt.timezone.utc))
    assert len(pending) == 0


def test_record_idempotent_on_version(state_db):
    """Re-inserting same version is a no-op (UNIQUE constraint)."""
    from trading_bot.lab_promotions import LabPromotionStore

    store = LabPromotionStore(state_db)
    promoted = dt.datetime(2026, 4, 28, 10, 1, tzinfo=dt.timezone.utc)
    store.record(promoted_at=promoted, version="v-1", template="momentum",
                 git_sha="x", fitness=1.0, params={}, risk_caps={})
    # Second call — should not raise, should not duplicate.
    store.record(promoted_at=promoted, version="v-1", template="momentum",
                 git_sha="x", fitness=1.0, params={}, risk_caps={})

    pending = store.pending_validation(now=dt.datetime(2026, 4, 28, 22, 0,
                                                       tzinfo=dt.timezone.utc))
    assert len(pending) == 1


def test_update_validation_counts(state_db):
    from trading_bot.lab_promotions import LabPromotionStore

    store = LabPromotionStore(state_db)
    store.record(
        promoted_at=dt.datetime(2026, 4, 28, 10, 1, tzinfo=dt.timezone.utc),
        version="v-x", template="momentum", git_sha="x", fitness=1.0,
        params={}, risk_caps={},
    )
    store.update_counts(version="v-x", scans=12, entries=3, near_misses=5)

    pending = store.pending_validation(now=dt.datetime(2026, 4, 28, 22, 0,
                                                       tzinfo=dt.timezone.utc))
    assert pending[0]["scans_since_promote"] == 12
    assert pending[0]["entries_since_promote"] == 3
    assert pending[0]["near_misses_since_promote"] == 5
