"""LabPromotionStore — tracks each lab strategy promotion + first-24h
validation counts. Surfaces in the daily digest under "New Strategy"."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


class LabPromotionStore:
    def __init__(self, db_path: Path | str = "data/state.db") -> None:
        self._engine = create_engine(f"sqlite:///{db_path}", future=True)

    def record(self, *, promoted_at: dt.datetime, version: str,
               template: str, git_sha: str, fitness: float,
               params: dict[str, Any], risk_caps: dict[str, Any]) -> None:
        """Idempotent on `version` — duplicates are silently ignored."""
        with self._engine.begin() as c:
            c.execute(
                text(
                    "INSERT OR IGNORE INTO lab_promotions "
                    "(promoted_at, version, template, git_sha, fitness_at_promotion, "
                    " params_json, risk_caps_json) "
                    "VALUES (:promoted_at, :version, :template, :git_sha, :fitness, "
                    "        :params, :risk_caps)"
                ),
                {
                    "promoted_at": promoted_at, "version": version,
                    "template": template, "git_sha": git_sha, "fitness": fitness,
                    "params": json.dumps(params), "risk_caps": json.dumps(risk_caps),
                },
            )

    def pending_validation(self, *, now: dt.datetime) -> list[dict[str, Any]]:
        """Promotions whose first-24h validation window is still open
        (validated_at IS NULL AND promoted_at + 24h > now)."""
        cutoff = now - dt.timedelta(hours=24)
        with self._engine.begin() as c:
            rows = c.execute(
                text(
                    "SELECT promoted_at, version, template, git_sha, "
                    "       fitness_at_promotion, params_json, risk_caps_json, "
                    "       scans_since_promote, entries_since_promote, "
                    "       near_misses_since_promote "
                    "FROM lab_promotions "
                    "WHERE validated_at IS NULL AND promoted_at > :cutoff "
                    "ORDER BY promoted_at DESC"
                ),
                {"cutoff": cutoff},
            ).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d.pop("params_json"))
            d["risk_caps"] = json.loads(d.pop("risk_caps_json"))
            out.append(d)
        return out

    def update_counts(self, *, version: str, scans: int, entries: int,
                      near_misses: int) -> None:
        with self._engine.begin() as c:
            c.execute(
                text(
                    "UPDATE lab_promotions SET "
                    "scans_since_promote = :scans, "
                    "entries_since_promote = :entries, "
                    "near_misses_since_promote = :near_misses "
                    "WHERE version = :version"
                ),
                {"version": version, "scans": scans, "entries": entries,
                 "near_misses": near_misses},
            )

    def mark_validated(self, *, version: str, validated_at: dt.datetime) -> None:
        with self._engine.begin() as c:
            c.execute(
                text("UPDATE lab_promotions SET validated_at = :v WHERE version = :ver"),
                {"v": validated_at, "ver": version},
            )

    def latest(self) -> dict[str, Any] | None:
        with self._engine.begin() as c:
            row = c.execute(
                text("SELECT * FROM lab_promotions ORDER BY promoted_at DESC LIMIT 1")
            ).mappings().first()
        return dict(row) if row else None
