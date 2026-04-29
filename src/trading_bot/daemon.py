"""Daemon entrypoint. Long-running process under launchd.

Usage:
    python -m trading_bot.daemon

Reads paper_active.json, runs Alembic migrations, registers APScheduler
jobs, runs forever. Heartbeat fires every cadence.heartbeat_seconds.
On SIGTERM, gracefully stops scheduler and exits 0.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from trading_bot.cadence import load_cadence
from trading_bot.log_structured import StructuredLogger
from trading_bot.scheduler_jobs import register_jobs
from trading_bot.state_heartbeat import write_heartbeat
from trading_bot.state_pause import is_paused


CONFIG_PATH = Path(os.environ.get("TRADING_BOT_CONFIG", "data/paper_active.json"))
HEARTBEAT_PATH = Path(os.environ.get("TRADING_BOT_HEARTBEAT", "data/heartbeat.json"))
PAUSE_PATH = Path(os.environ.get("TRADING_BOT_PAUSE", "data/pause.flag"))
RUNS_DIR = Path(os.environ.get("TRADING_BOT_RUNS", "runs"))
STATE_DB = Path(os.environ.get("TRADING_BOT_STATE_DB", "data/state.db"))


def _build_wheel_deps(*, settings, app_cfg, state_engine, alpaca_client,
                      risk_manager, intelligence_macro, regime_detector,
                      queue_alert):
    """Construct a WheelDeps bag for the wheel scanner / manager.

    All wheel-runner external IO clients are imported lazily here so the
    daemon doesn't need them until wheel.enabled flips True. Keeps cold
    boot fast and avoids a Finnhub/ApeWisdom dep being load-bearing for
    paper-with-wheel-disabled deployments.
    """
    import datetime as dt
    from trading_bot.intelligence_apewisdom import ApeWisdomClient
    from trading_bot.intelligence_finnhub import FinnhubClient
    from trading_bot.options.alpaca_options import OptionAlpacaClient
    from trading_bot.options.iv_rank import compute_iv_rank
    from trading_bot.options.wheel_runner import WheelDeps
    from trading_bot.options.wheel_signals import (
        produce_candidates, SignalDeps, _read_last_iv,
    )

    finnhub = FinnhubClient(api_key=settings.finnhub_api_key)
    ape = ApeWisdomClient()
    opt = OptionAlpacaClient(settings)

    def _load_yaml_symbols(path_str: str) -> set[str]:
        try:
            import yaml
            p = Path(path_str)
            if not p.exists():
                return set()
            data = yaml.safe_load(p.read_text()) or {}
            return {str(s).upper() for s in (data.get("symbols") or [])}
        except Exception:
            return set()

    def _eligible_set() -> set[str]:
        """Wheel-eligible universe = discovered set (wheel_universe_cache,
        eligible=True) plus operator allowlist override, minus blocklist
        override, intersected with currently-optionable. The discovered set
        is built nightly by wheel_universe_builder. If the cache is empty
        (first-ever run hasn't completed), falls back to allowlist-only.
        Signals decide which of these to act on TODAY."""
        from trading_bot.state_db import WheelUniverseCache
        from sqlalchemy.orm import Session as _S
        optionable = opt.list_optionable_us_equities()
        blocklist = _load_yaml_symbols(app_cfg.wheel.blocklist_path)
        allowlist = _load_yaml_symbols(app_cfg.wheel.allowlist_path)
        with _S(state_engine) as _s:
            discovered = {
                r.symbol for r in
                _s.query(WheelUniverseCache).filter_by(eligible=True).all()
            }
        if not discovered and not allowlist:
            return set()  # no discovered universe and no override → opt in required
        return ((discovered | allowlist) & optionable) - blocklist

    def _sentiment_for(symbol: str) -> float | None:
        try:
            from trading_bot.news_sentiment import latest_score_for
            return latest_score_for(symbol)
        except Exception:
            return None

    def _spot_for(symbol: str) -> float | None:
        try:
            from trading_bot.market_data import MarketDataClient
            md = MarketDataClient(settings)
            df = md.get_daily_bars(symbol, lookback_days=2)
            if df.empty:
                return None
            return float(df["close"].iloc[-1])
        except Exception:
            return None

    def _candidates_for_today():
        """Signal-driven candidate sourcing — runs against the eligible set
        and produces a small ranked list of symbols backed by an actual
        signal (post-earnings / stable-elevated-IV). Empty if VIX out-of-band
        or no signals fire."""
        return produce_candidates(
            SignalDeps(
                finnhub=finnhub, iv_engine=state_engine,
                sentiment_for=_sentiment_for,
                macro_snapshotter=intelligence_macro,
                today=dt.date.today(),
            ),
            eligible=_eligible_set(),
            cfg=app_cfg.wheel,
        )

    def _iv_rank_for(symbol: str) -> float | None:
        last_iv = _read_last_iv(state_engine, symbol)
        if last_iv is None:
            return None
        return compute_iv_rank(state_engine, symbol, current_iv=last_iv, min_history=5)

    return WheelDeps(
        cfg=app_cfg.wheel, engine=state_engine, option_alpaca=opt,
        alpaca_client=alpaca_client, risk_manager=risk_manager,
        intelligence_macro=intelligence_macro, regime_detector=regime_detector,
        candidates_for_today=_candidates_for_today, iv_rank_for=_iv_rank_for,
        spot_for=_spot_for, sentiment_for=_sentiment_for,
        finnhub=finnhub, apewisdom=ape, alert_queue=queue_alert,
    )


def _build_iv_capture_runner(*, settings, app_cfg, state_engine):
    """Build a callable for the daily iv_capture cron job. Returns a no-op if
    wheel disabled — no chain fetches without operator opt-in."""
    def _runner():
        if not app_cfg.wheel.enabled:
            return
        import datetime as dt
        from sqlalchemy.orm import Session as _S
        from trading_bot.options.alpaca_options import OptionAlpacaClient
        from trading_bot.options.iv_capture import IvCaptureDeps, run_iv_capture
        from trading_bot.market_data import MarketDataClient
        from trading_bot.state_db import WheelUniverseCache
        opt = OptionAlpacaClient(settings)
        md = MarketDataClient(settings)
        # Eligible set: discovered (wheel_universe_cache) + allowlist override,
        # minus blocklist, intersected with currently optionable.
        try:
            import yaml
            p = Path(app_cfg.wheel.allowlist_path)
            allow = {str(s).upper() for s in (yaml.safe_load(p.read_text()) or {}).get("symbols", [])} if p.exists() else set()
            p_b = Path(app_cfg.wheel.blocklist_path)
            block = {str(s).upper() for s in (yaml.safe_load(p_b.read_text()) or {}).get("symbols", [])} if p_b.exists() else set()
        except Exception:
            allow, block = set(), set()
        with _S(state_engine) as _s:
            discovered = {
                r.symbol for r in
                _s.query(WheelUniverseCache).filter_by(eligible=True).all()
            }
        eligible = ((discovered | allow) & opt.list_optionable_us_equities()) - block
        if not eligible:
            return
        def _spot(s: str) -> float | None:
            try:
                df = md.get_daily_bars(s, lookback_days=2)
                if df.empty:
                    return None
                return float(df["close"].iloc[-1])
            except Exception:
                return None
        deps = IvCaptureDeps(
            option_alpaca=opt, engine=state_engine,
            spot_for=_spot,
            eligible=eligible, today=dt.date.today(),
        )
        run_iv_capture(deps)
    return _runner


def _build_universe_builder_runner(*, settings, app_cfg, state_engine):
    """Build a callable for the nightly wheel-universe build job. Walks the
    optionable universe, filters via Finnhub, writes to wheel_universe_cache.
    First-ever run is ~100 min (Finnhub free 60/min × ~6,000 names);
    subsequent runs only re-check 14d-stale entries (~7 min)."""
    def _runner():
        if not app_cfg.wheel.enabled:
            return
        import datetime as dt
        from trading_bot.intelligence_finnhub import FinnhubClient
        from trading_bot.options.alpaca_options import OptionAlpacaClient
        from trading_bot.options.wheel_universe_builder import (
            BuilderDeps, run_universe_build,
        )
        try:
            import yaml
            p = Path(app_cfg.wheel.allowlist_path)
            allow = {str(s).upper() for s in (yaml.safe_load(p.read_text()) or {}).get("symbols", [])} if p.exists() else set()
            p_b = Path(app_cfg.wheel.blocklist_path)
            block = {str(s).upper() for s in (yaml.safe_load(p_b.read_text()) or {}).get("symbols", [])} if p_b.exists() else set()
        except Exception:
            allow, block = set(), set()
        opt = OptionAlpacaClient(settings)
        finnhub = FinnhubClient(api_key=settings.finnhub_api_key)
        deps = BuilderDeps(
            engine=state_engine,
            optionable_set=opt.list_optionable_us_equities(),
            finnhub=finnhub, blocklist=block, allowlist=allow,
            today=dt.date.today(),
        )
        run_universe_build(deps)
    return _runner


class _MacroSnapshotter:
    """Adapter: wheel_runner expects an object with .snapshot() returning a
    MacroSnapshot. The base FRED helper is a free function, so wrap it."""

    def snapshot(self):
        from trading_bot.intelligence import get_macro_snapshot
        return get_macro_snapshot()


class _RegimeDetectorAdapter:
    """Adapter: wheel_runner expects regime_detector.detect() → str."""

    def __init__(self, settings, app_cfg):
        self._settings = settings
        self._cfg = app_cfg

    def detect(self) -> str:
        from trading_bot.market_data import MarketDataClient
        from trading_bot.intelligence import get_macro_snapshot
        from trading_bot.regime import detect_regime
        try:
            market = MarketDataClient(self._settings)
            try:
                vix = get_macro_snapshot().vix
            except Exception:
                vix = None
            reading = detect_regime(
                market, vix=vix,
                vol_threshold_pct=self._cfg.regime.vol_threshold_pct,
            )
            return reading.regime.value
        except Exception:
            return "sideways"


def _load_runners(log: StructuredLogger):
    """Instantiate Role objects and return runner callables that wrap role.safe_run(ctx)."""
    import trading_bot.cli as cli_mod
    from trading_bot.state_db import get_engine
    from trading_bot.roles.health_pulse import HealthPulseRole
    from trading_bot.roles.stock_scanner import StockScannerRole
    from trading_bot.roles.crypto_scanner import CryptoScannerRole
    from trading_bot.roles.portfolio_monitor import PortfolioMonitorRole
    from trading_bot.roles.order_steward import OrderStewardRole
    from trading_bot.roles.sentiment_analyst import SentimentAnalystRole
    from trading_bot.roles.universe_curator import UniverseCuratorRole
    from trading_bot.roles.vip_listener import VipListenerRole
    from trading_bot.roles.reporter import ReporterRole
    from trading_bot.roles.strategy_coach import StrategyCoachRole
    from trading_bot.roles.hold_spy_coordinator import HoldSpyCoordinatorRole
    from trading_bot.state_fallback import bootstrap_if_empty
    from sqlalchemy.orm import Session as _Sess
    from trading_bot.log_rotation import rotate_logs

    config_version = "phase4-v1"

    # Build the engine once — roles hold it for KPI persistence across calls.
    engine = get_engine(STATE_DB)

    # Phase 4: bootstrap fallback flag with active=0 so scanners have a known start state.
    # Tolerant of missing schema (integration tests with TRADING_BOT_SKIP_MIGRATIONS=1).
    try:
        with _Sess(engine) as _s:
            bootstrap_if_empty(_s)
    except Exception as e:
        log.event("fallback_flag_bootstrap_skipped", error=str(e))

    # Instantiate Role objects once (not per call) so SQLAlchemy engine is stable.
    health_pulse = HealthPulseRole(engine=engine, heartbeat_path=HEARTBEAT_PATH, version=config_version)
    stock_scanner = StockScannerRole(engine=engine)
    crypto_scanner = CryptoScannerRole(engine=engine)
    portfolio_monitor = PortfolioMonitorRole(engine=engine)
    order_steward = OrderStewardRole(engine=engine)
    sentiment_analyst = SentimentAnalystRole(engine=engine)
    universe_curator = UniverseCuratorRole(engine=engine)
    vip_listener = VipListenerRole(engine=engine)
    reporter = ReporterRole(engine=engine)
    strategy_coach = StrategyCoachRole(engine=engine)
    hold_spy_coordinator = HoldSpyCoordinatorRole(engine=engine)

    def _heartbeat():
        health_pulse.safe_run(ctx={})
        # Also write the legacy heartbeat file so supervisor's StallDetector still works.
        write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action="heartbeat")

    def _wrap(name: str, role_fn):
        """Wrap a role callable with pause-flag check and heartbeat update."""
        def runner():
            log.event(f"{name}_start")
            # Block any job that may place orders.
            # midday_snapshot is informational only (no orders placed).
            # daily_digest invokes eod-report (read-only) so it is safe during pause.
            if is_paused(PAUSE_PATH) and name in {"intel_scan", "crypto_scan"}:
                log.event(f"{name}_skipped", reason="pause.flag set")
                write_heartbeat(HEARTBEAT_PATH, version=config_version,
                                last_action=f"{name}_skipped_paused")
                return
            try:
                role_fn()
                log.event(f"{name}_finish")
            except Exception as e:
                log.error(f"{name}_failed", error=e)
            finally:
                write_heartbeat(HEARTBEAT_PATH, version=config_version, last_action=name)
        return runner

    # Build wheel deps lazily — only when wheel is enabled in config — so a
    # paper-with-wheel-disabled deployment doesn't need Finnhub/ApeWisdom
    # creds and doesn't fail if Alpaca options data is unreachable.
    wheel_scan_runner = lambda: log.event(
        "wheel_scan_stub", reason="wheel disabled or wiring failed"
    )
    wheel_manage_runner = lambda: log.event(
        "wheel_manage_stub", reason="wheel disabled or wiring failed"
    )
    iv_capture_runner = lambda: log.event(
        "iv_capture_stub", reason="wheel disabled or wiring failed"
    )
    wheel_universe_build_runner = lambda: log.event(
        "wheel_universe_build_stub", reason="wheel disabled or wiring failed"
    )
    reconcile_options_callable = None
    try:
        from trading_bot.config import Settings, load_config
        from trading_bot.alpaca_client import AlpacaClient
        from trading_bot.risk_manager import RiskManager
        from trading_bot.alerts import queue_alert as _queue_alert
        _settings = Settings()
        _app_cfg = load_config(Path("strategy/config.yaml"))
        if _app_cfg.wheel.enabled:
            from trading_bot.options.wheel_runner import (
                run_wheel_scan, run_wheel_manage,
            )
            _alpaca = AlpacaClient(_settings)
            _risk = RiskManager(_app_cfg)
            _macro = _MacroSnapshotter()
            _reg = _RegimeDetectorAdapter(_settings, _app_cfg)
            _wheel_deps = _build_wheel_deps(
                settings=_settings, app_cfg=_app_cfg,
                state_engine=engine, alpaca_client=_alpaca,
                risk_manager=_risk, intelligence_macro=_macro,
                regime_detector=_reg, queue_alert=_queue_alert,
            )
            wheel_scan_runner = lambda: run_wheel_scan(_wheel_deps)
            wheel_manage_runner = lambda: run_wheel_manage(_wheel_deps)
            iv_capture_runner = _build_iv_capture_runner(
                settings=_settings, app_cfg=_app_cfg, state_engine=engine,
            )
            wheel_universe_build_runner = _build_universe_builder_runner(
                settings=_settings, app_cfg=_app_cfg, state_engine=engine,
            )

            # reconcile_options runs after the equity reconciler so option
            # cycle state is reconciled in the same pass.
            from trading_bot.options.alpaca_options import OptionAlpacaClient
            from trading_bot.reconciler import reconcile_options as _rec_opts
            _opt = OptionAlpacaClient(_settings)

            def _run_reconcile_options() -> None:
                try:
                    _rec_opts(
                        engine=engine, option_alpaca=_opt,
                        alpaca_equity=_alpaca, alert_queue=_queue_alert,
                    )
                except Exception as e:
                    log.error("reconcile_options_failed", error=e)

            reconcile_options_callable = _run_reconcile_options
        else:
            log.event("wheel_disabled", reason="config.wheel.enabled=false")
    except Exception as e:
        log.event("wheel_wiring_skipped", reason=str(e))

    def _reconciler_runner() -> None:
        cli_mod.reconcile_cli.callback()
        if reconcile_options_callable is not None:
            reconcile_options_callable()

    return {
        "heartbeat": _heartbeat,
        "intel_scan": _wrap("intel_scan", lambda: stock_scanner.safe_run(ctx={})),
        "crypto_scan": _wrap("crypto_scan", lambda: crypto_scanner.safe_run(ctx={})),
        "portfolio_watch": _wrap("portfolio_watch", lambda: portfolio_monitor.safe_run(ctx={})),
        "verify_stops": _wrap("verify_stops", lambda: order_steward.safe_run(ctx={})),
        "news_warm": _wrap("news_warm", lambda: sentiment_analyst.safe_run(ctx={})),
        "massive_refresh": _wrap("massive_refresh", lambda: universe_curator.run_refresh(ctx={})),
        "premarket_rank": _wrap("premarket_rank", lambda: universe_curator.run_rank(ctx={})),
        # Midday rerank: re-pulls Polygon grouped (running intraday-aggregated daily
        # bar for every US ticker) + re-runs Stage-1/2. Captures symbols that broke
        # out this morning so the 12:30 stock scan can act on them.
        "midday_rerank": _wrap(
            "midday_rerank",
            lambda: (
                universe_curator.run_refresh(ctx={}),
                universe_curator.run_rank(ctx={}),
            ),
        ),
        "vip_scan": _wrap("vip_scan", lambda: vip_listener.safe_run(ctx={})),
        "midday_snapshot": _wrap("midday_snapshot", lambda: cli_mod.midday_snapshot_cli.callback()),
        "daily_digest": _wrap("daily_digest", lambda: reporter.run_eod(ctx={})),
        "log_rotation": _wrap("log_rotation", lambda: rotate_logs(runs_dir=RUNS_DIR, keep_days=90)),
        "strategy_coach": _wrap("strategy_coach", lambda: strategy_coach.safe_run(ctx={})),
        "hold_spy_coordinator": _wrap(
            "hold_spy_coordinator", lambda: hold_spy_coordinator.safe_run(ctx={})
        ),
        "reconciler": _wrap("reconciler", _reconciler_runner),
        "schedule_audit": _wrap("schedule_audit", lambda: cli_mod.schedule_audit_cli.callback()),
        "alert_drain": _wrap("alert_drain", lambda: cli_mod.alert_drain_cli.callback()),
        "wheel_scan": _wrap("wheel_scan", wheel_scan_runner),
        "wheel_manage": _wrap("wheel_manage", wheel_manage_runner),
        "iv_capture": _wrap("iv_capture", iv_capture_runner),
        "wheel_universe_build": _wrap("wheel_universe_build", wheel_universe_build_runner),
    }


def main() -> int:
    log = StructuredLogger(base=RUNS_DIR, role="daemon")

    # Auto-apply pending migrations on boot. Idempotent — exits clean if up-to-date.
    # Set TRADING_BOT_SKIP_MIGRATIONS=1 to skip (used by integration tests that
    # set up their own schema via SQLAlchemy directly).
    if os.environ.get("TRADING_BOT_SKIP_MIGRATIONS") != "1":
        try:
            import subprocess
            repo_root = Path(__file__).parent.parent.parent  # src/trading_bot/daemon.py → repo root
            result = subprocess.run(
                [str(repo_root / ".venv" / "bin" / "alembic"),
                 "-c", str(repo_root / "migrations" / "alembic.ini"),
                 "upgrade", "head"],
                capture_output=True, text=True, timeout=30, cwd=str(repo_root),
            )
            if result.returncode != 0:
                log.error("alembic_upgrade_failed", error=RuntimeError(result.stderr))
                return 1
            log.event("alembic_upgrade", result="ok")
        except Exception as e:
            log.error("alembic_upgrade_exception", error=e)
            return 1
    else:
        log.event("alembic_upgrade_skipped", reason="TRADING_BOT_SKIP_MIGRATIONS=1")

    log.event("daemon_boot", config_path=str(CONFIG_PATH))

    if not CONFIG_PATH.exists():
        log.error(
            "daemon_no_config",
            error=FileNotFoundError(f"config missing: {CONFIG_PATH}"),
        )
        return 1

    cadence = load_cadence(CONFIG_PATH)
    log.event("cadence_loaded",
              heartbeat=cadence.heartbeat_seconds,
              stock_scanner_minutes=cadence.stock_scanner_minutes)

    sched = BackgroundScheduler(timezone="America/New_York")
    runners = _load_runners(log)
    register_jobs(scheduler=sched, cadence=cadence, runners=runners)

    stop = {"flag": False}

    def _stop_handler(signum, frame):
        log.event("daemon_stopping", signal=signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    # Initial heartbeat before scheduler runs (so supervisor doesn't see stale boot)
    runners["heartbeat"]()

    sched.start()
    log.event("scheduler_started", jobs=[j.id for j in sched.get_jobs()])

    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        log.event("daemon_stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
