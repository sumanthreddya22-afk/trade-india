"""Calibrator — Tier 5 lab role (Role 21).

Daily Spearman rank correlation between the active config's predicted per-trade
P&L (from the most recent backtest fold) and realized per-trade P&L from paper
trades closed in the rolling 30-trade / 30-day window. On HIGH-severity drift
(corr < 0.3), halts Promoter for 7 days — the backtest model has decoupled
from real conditions and promotion based on it would be unsafe.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.calibration import compute_drift_score
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import (
    CalibrationRun,
    Leaderboard,
    PromoterHalt,
    RoleRun,
)

CONFIG_PATH_DEFAULT = Path("data/paper_active.json")
CLOSED_TRADES_DB_DEFAULT = Path("data/closed_trades.db")
LOOKBACK_TRADES = 30
LOOKBACK_DAYS = 30
HIGH_SEVERITY_HALT_DAYS = 7


class CalibratorRole(BaseRole):
    name = "calibrator"
    tier = 5
    process = "lab"
    job_description = (
        "Daily Spearman corr of backtest predicted vs paper realized P&L. "
        "Halts Promoter on severe drift."
    )
    sla_seconds = 60
    upstream_roles = ["param_optimizer"]
    downstream_roles = ["promoter", "reporter"]

    def __init__(
        self,
        *,
        engine,
        config_path: str | Path = CONFIG_PATH_DEFAULT,
        closed_trades_db: str | Path = CLOSED_TRADES_DB_DEFAULT,
    ):
        super().__init__(engine=engine)
        self.config_path = Path(config_path)
        self.closed_trades_db = Path(closed_trades_db)

    def _do_work(self, ctx):
        # 1. Resolve template name from active config (or ctx override).
        template = ctx.get("template") or self._read_active_template()
        if template is None:
            return {"skipped": True, "reason": "no_active_config"}

        # 2. Most recent leaderboard row for this template with predictions.
        with Session(self.engine) as session:
            row = (
                session.query(Leaderboard)
                .filter(
                    Leaderboard.template_name == template,
                    Leaderboard.per_trade_predictions_json.isnot(None),
                )
                .order_by(Leaderboard.recorded_at.desc())
                .first()
            )
        if row is None:
            self._write_run(template, n=0, corr=None, severity="insufficient_data")
            return {
                "skipped": True,
                "reason": "no_leaderboard_with_predictions",
                "template": template,
            }
        predictions = json.loads(row.per_trade_predictions_json)

        # 3. Realized trades from rolling window.
        realized = self._load_realized_trades()
        if not realized:
            self._write_run(template, n=0, corr=None, severity="insufficient_data")
            return {
                "skipped": True,
                "reason": "no_closed_trades",
                "template": template,
            }

        # 4. Pair by symbol (closest entry-date match).
        pairs = _pair_trades(predictions, realized)
        n_pairs = len(pairs)
        if n_pairs == 0:
            self._write_run(template, n=0, corr=None, severity="insufficient_data")
            return {"skipped": True, "reason": "no_pairs", "template": template}

        pred_pnls = [p[0] for p in pairs]
        real_pnls = [p[1] for p in pairs]
        corr, severity = compute_drift_score(pred_pnls, real_pnls)
        self._write_run(template, n=n_pairs, corr=corr, severity=severity)

        # 5. On HIGH severity, halt Promoter for 7 days.
        if severity == "high":
            now = dt.datetime.now(dt.timezone.utc)
            with Session(self.engine) as session:
                session.add(
                    PromoterHalt(
                        halted_until=now
                        + dt.timedelta(days=HIGH_SEVERITY_HALT_DAYS),
                        reason=(
                            f"calibrator drift: spearman_corr={corr:.3f} on "
                            f"{n_pairs} trades — backtest no longer predictive"
                        ),
                        set_by="calibrator",
                        set_at=now,
                    )
                )
                session.commit()

        return {
            "template": template,
            "n_trades": n_pairs,
            "spearman_corr": corr,
            "severity": severity,
        }

    def _read_active_template(self) -> str | None:
        if not self.config_path.exists():
            return None
        try:
            cfg = json.loads(self.config_path.read_text())
            # Accept either Phase 3 'active_template' or the bootstrap shape.
            return cfg.get("active_template") or "momentum"
        except Exception:
            return None

    def _load_realized_trades(self) -> list[dict]:
        """Read recent closed trades, return list of dicts {symbol, entry_date, realized_pnl}."""
        if not self.closed_trades_db.exists():
            return []
        from trading_bot.reconciliation import ClosedTradeStore

        store = ClosedTradeStore(self.closed_trades_db)
        all_trades = store.all()
        if not all_trades:
            return []
        # Take last LOOKBACK_TRADES OR everything within LOOKBACK_DAYS, whichever is fewer.
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)
        windowed = [t for t in all_trades if _ensure_aware(t.exit_time) >= cutoff]
        # Most recent up to LOOKBACK_TRADES
        windowed = windowed[-LOOKBACK_TRADES:]
        return [
            {
                "symbol": t.symbol,
                "entry_date": t.entry_time.date().isoformat(),
                "realized_pnl": float(t.realized_pnl),
            }
            for t in windowed
        ]

    def _write_run(
        self, template: str, *, n: int, corr: float | None, severity: str
    ) -> None:
        with Session(self.engine) as session:
            session.add(
                CalibrationRun(
                    recorded_at=dt.datetime.now(dt.timezone.utc),
                    template_name=template,
                    n_trades=n,
                    spearman_corr=corr,
                    severity=severity,
                )
            )
            session.commit()

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "calibration_runs",
            float(count),
            f"{count} calibration runs in last {lookback_days}d",
        )


def _ensure_aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _pair_trades(predictions: list[dict], realized: list[dict]) -> list[tuple[float, float]]:
    """Match each realized trade to the closest-by-entry-date predicted trade for the same symbol.

    Returns paired (predicted_pnl, realized_pnl) tuples. Realized trades with no
    matching symbol in predictions are skipped.
    """
    # Group predictions by symbol → list of (entry_date, pnl)
    by_symbol: dict[str, list[tuple[dt.date, float]]] = {}
    for p in predictions:
        try:
            d = dt.date.fromisoformat(p["entry_date"])
        except Exception:
            continue
        by_symbol.setdefault(p["symbol"], []).append((d, float(p["predicted_pnl"])))

    pairs: list[tuple[float, float]] = []
    for r in realized:
        sym = r["symbol"]
        if sym not in by_symbol:
            continue
        try:
            real_date = dt.date.fromisoformat(r["entry_date"])
        except Exception:
            continue
        # Closest predicted trade for this symbol by date
        best = min(by_symbol[sym], key=lambda x: abs((x[0] - real_date).days))
        pairs.append((best[1], r["realized_pnl"]))
    return pairs
