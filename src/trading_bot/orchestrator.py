import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_bot.shared.alpaca_client import (
    AlpacaClient,
    AssetClass,
    OrderRequest,
    OrderSide,
)
from trading_bot.shared.config import AppConfig
from trading_bot.exceptions import AlpacaClientError, RiskRuleViolation
from trading_bot.market_data import (
    MIN_BARS_FOR_INDICATORS,
    MarketDataClient,
    compute_indicators,
)
from trading_bot.shared.risk_manager import RiskManager, RiskState
from trading_bot.state import WatchlistEntry, has_open_position
from trading_bot.strategy import MomentumStrategy, SignalAction, strategy_for_regime
from trading_bot.trade_journal import TradeJournal, TradeRecord


# ---------------------------------------------------------------------------
# Decision schema (W1.1 — PDF-prescribed strict JSON contract).
# Every field has a default so legacy two-arg call sites (Decision(symbol, action))
# continue to work unchanged. "Unknown" is represented as None / empty.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskAfter:
    trade_var: Decimal | None = None
    portfolio_var_after: Decimal | None = None
    expected_shortfall_after: Decimal | None = None
    gross_after: Decimal | None = None
    net_after: Decimal | None = None
    drawdown_state: Decimal | None = None


@dataclass(frozen=True)
class ComplianceFlags:
    approved_instrument: bool | None = None
    approved_venue: bool | None = None
    restricted_list_clear: bool | None = None
    mnpi_clear: bool | None = None
    market_abuse_clear: bool | None = None


@dataclass(frozen=True)
class DataQualityFlags:
    fresh: bool | None = None
    complete: bool | None = None
    aligned: bool | None = None
    provenance_ok: bool | None = None


@dataclass(frozen=True)
class ExecutionConstraints:
    price_collar_ok: bool | None = None
    size_collar_ok: bool | None = None
    max_participation: Decimal | None = None


@dataclass(frozen=True)
class AuditObject:
    policy_version: str = ""
    strategy_version: str = ""
    model_versions: dict[str, str] = field(default_factory=dict)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    data_snapshot_ids: tuple[str, ...] = ()
    regime: str = ""
    risk_state_id: str = ""
    timestamp_utc: str = ""


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: str
    reason: str = ""
    entry_order_id: str = ""
    stop_loss_order_id: str = ""
    confidence: float | None = None
    expected_edge_bps: float | None = None
    risk_after: RiskAfter = field(default_factory=RiskAfter)
    compliance: ComplianceFlags = field(default_factory=ComplianceFlags)
    data_quality: DataQualityFlags = field(default_factory=DataQualityFlags)
    execution_constraints: ExecutionConstraints = field(default_factory=ExecutionConstraints)
    alerts: tuple[str, ...] = ()
    audit: AuditObject = field(default_factory=AuditObject)


def _serialize_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    return v


def _serialize_dataclass(obj: Any) -> dict[str, Any]:
    return {
        f.name: _serialize_value(getattr(obj, f.name))
        for f in obj.__dataclass_fields__.values()
    }


def decision_to_dict(d: Decision) -> dict[str, Any]:
    """Render a Decision as the PDF's strict JSON output contract.

    Decimal → string (lossless), tuples → lists, sub-objects → nested dicts.
    Result is safe to pass to ``json.dumps`` with no custom encoder.
    """
    return {
        "symbol": d.symbol,
        "action": d.action,
        "reason": d.reason,
        "entry_order_id": d.entry_order_id,
        "stop_loss_order_id": d.stop_loss_order_id,
        "confidence": d.confidence,
        "expected_edge_bps": d.expected_edge_bps,
        "risk_after": _serialize_dataclass(d.risk_after),
        "compliance": _serialize_dataclass(d.compliance),
        "data_quality": _serialize_dataclass(d.data_quality),
        "execution_constraints": _serialize_dataclass(d.execution_constraints),
        "alerts": list(d.alerts),
        "audit": {
            "policy_version": d.audit.policy_version,
            "strategy_version": d.audit.strategy_version,
            "model_versions": dict(d.audit.model_versions),
            "prompt_versions": dict(d.audit.prompt_versions),
            "data_snapshot_ids": list(d.audit.data_snapshot_ids),
            "regime": d.audit.regime,
            "risk_state_id": d.audit.risk_state_id,
            "timestamp_utc": d.audit.timestamp_utc,
        },
    }


@dataclass(frozen=True)
class ScanResult:
    decisions: list[Decision]
    timestamp: datetime


def load_ranked_watchlist(path: Path) -> list[WatchlistEntry]:
    """Parse strategy/opportunities.md and return WatchlistEntry list in rank order.

    Entries look like:
        ### 1. NVDA (us_equity)
        ### 2. BTC/USD (crypto)

    Bucket B: ONLY parses entries in the ``## Ranked Candidates`` section.
    The stage-1 shortlist (under ``## Stage-1 Shortlist (no lane endorsements)``)
    is informational and must NOT be auto-traded — those names had zero
    stage-2 lane confirmation. Previous regex matched any ``### N. SYM``
    heading, accidentally pulling shortlist rows into the trade lane.
    """
    if not path.exists():
        return []
    text = path.read_text()
    # Slice between "## Ranked Candidates" and the next "## " heading.
    start = text.find("## Ranked Candidates")
    if start == -1:
        return []
    body_start = start + len("## Ranked Candidates")
    rest = text[body_start:]
    next_section = re.search(r"^##\s", rest, re.MULTILINE)
    body = rest[: next_section.start()] if next_section else rest
    out: list[WatchlistEntry] = []
    pattern = re.compile(r"^###\s+\d+\.\s+(\S+)\s+\(([^)]+)\)\s*$", re.MULTILINE)
    for match in pattern.finditer(body):
        symbol = match.group(1)
        asset_class_raw = match.group(2)
        # Preserve the raw asset_class value; only normalise crypto variants.
        if "crypto" in asset_class_raw.lower():
            asset_class = "crypto"
        else:
            asset_class = asset_class_raw
        out.append(WatchlistEntry(symbol=symbol, asset_class=asset_class, notes=""))
    return out


def opportunities_age_hours(path: Path) -> float | None:
    """Return age of strategy/opportunities.md in hours, parsed from the
    ``Generated:`` line at the top. Returns None if missing/unparseable.
    Bucket B: callers use this to gate stale-data trades and to fire alerts.
    """
    if not path.exists():
        return None
    try:
        head = path.read_text()[:500]  # first ~500 chars cover the header
    except OSError:
        return None
    m = re.search(r"^Generated:\s*(\S+)", head, re.MULTILINE)
    if not m:
        return None
    try:
        ts_raw = m.group(1).replace("Z", "+00:00")
        ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


class TradeOrchestrator:
    def __init__(
        self,
        *,
        config: AppConfig,
        market_data: MarketDataClient,
        alpaca: AlpacaClient,
        journal: TradeJournal,
        regime: str = "trending_up",
        strategy: MomentumStrategy | None = None,
        risk_manager: RiskManager | None = None,
        state_builder=None,  # callable returning RiskState; if None, uses safe defaults
        decision_store=None,  # DecisionStore | None — when set, every Decision is persisted
        policy_version: str = "",
        restricted_list_path: Path | str | None = None,
        approved_venue_url: str | None = None,
        risk_debate_enabled: bool = False,
        risk_debate_engine=None,
        unblock_debate_enabled: bool = False,
        unblock_debate_engine=None,
        entry_debate_enabled: bool = False,
        entry_debate_engine=None,
        entry_debate_daily_cap: int = 50,
        intel_score_regime_override_threshold: float = 5.0,
        intel_lookup_engine=None,
    ) -> None:
        self._cfg = config
        self._market = market_data
        self._alpaca = alpaca
        self._journal = journal
        self._regime = regime
        # Strategy is chosen by regime if not explicitly provided
        self._strategy = strategy or strategy_for_regime(regime)
        # Wire the unblock-debate engine into RiskManager too — it's the same
        # state.db and we want the per_trade_risk_pct / max_position_pct
        # overrides consulted on every check. Tests that pass an explicit
        # ``risk_manager`` keep their existing wiring untouched.
        self._risk = risk_manager or RiskManager(config, engine=unblock_debate_engine)
        self._state_builder = state_builder
        self._decision_store = decision_store
        self._policy_version = policy_version
        # W2b compliance config — load once at init; reread on every scan via
        # _load_restricted_list() so the operator can edit YAML without restart.
        self._restricted_list_path = (
            Path(restricted_list_path) if restricted_list_path else None
        )
        self._approved_venue_url = approved_venue_url
        # Optional 3-way LLM risk debate (aggressive/conservative/neutral +
        # judge). OFF by default — debating every order would be cost-
        # prohibitive. When enabled, fires only on borderline operational
        # context (losing streak / size throttle / high per-trade VaR) and
        # fails open if the LLM is unavailable.
        self._risk_debate_enabled = risk_debate_enabled
        self._risk_debate_engine = risk_debate_engine
        # Phase 5.5/5.6 — Unblock committee on the OPPOSITE direction:
        # when the deterministic risk gate REJECTS a trade, the committee
        # may override and let it through. Fail-CLOSED (opposite of
        # risk_debate's fail-open). Requires a SQLAlchemy engine in
        # unblock_debate_engine for persisting the audit row.
        self._unblock_debate_enabled = unblock_debate_enabled
        self._unblock_debate_engine = unblock_debate_engine
        # Phase 6 — pre-trade entry debate. Fires on EVERY BUY signal that
        # passes the deterministic risk gate. Mailbox-routed (cheap) and
        # FAIL-SOFT: a debate failure (no creds, mailbox down, schema
        # mismatch) skips the trade AND queues an alert so the operator
        # knows the LLM gate was unreachable.
        self._entry_debate_enabled = entry_debate_enabled
        self._entry_debate_engine = entry_debate_engine
        self._entry_debate_daily_cap = entry_debate_daily_cap
        # Per-ticker intel-aware strategy resolution. When the orchestrator-
        # level regime returns no strategy (sideways/trending_down), a high
        # per-ticker intel_score can still unlock Momentum for that one
        # ticker. risk_off remains a hard wall regardless of intel.
        self._intel_score_regime_override_threshold = intel_score_regime_override_threshold
        # Engine for intel pool lookups. Defaults to the unblock-debate
        # engine since they're both the state.db engine in production.
        self._intel_lookup_engine = (
            intel_lookup_engine
            if intel_lookup_engine is not None
            else unblock_debate_engine
        )

    def _load_restricted_list(self) -> set[str]:
        if self._restricted_list_path is None:
            return set()
        from trading_bot.compliance import load_restricted_list
        return load_restricted_list(self._restricted_list_path)

    # ---- W1.4 decision emission helpers ------------------------------------

    def _strategy_name(self) -> str:
        if self._strategy is None:
            return "none"
        return type(self._strategy).__name__.replace("Strategy", "").lower() or "strategy"

    def _attach_audit(self, decision: "Decision") -> "Decision":
        """If the decision lacks audit metadata, attach a fresh audit object
        derived from orchestrator state (regime, strategy, policy_version)."""
        from trading_bot.shared.audit import build_audit
        if decision.audit.timestamp_utc:
            return decision  # caller already attached an audit
        a = build_audit(
            strategy=self._strategy_name(),
            regime=self._regime,
            policy_version=self._policy_version,
        )
        # dataclasses.replace preserves frozen-ness
        from dataclasses import replace
        return replace(decision, audit=a)

    @staticmethod
    def _normalise_asset_class(value) -> str:
        """Coerce an Alpaca AssetClass enum or arbitrary string into a
        canonical lowercase tag. The persistence layer should never see an
        ungainly ``"AssetClass.US_EQUITY"`` string from a bare ``str(enum)``."""
        if value is None:
            return ""
        v = getattr(value, "value", value)
        s = str(v).lower().strip()
        if s.startswith("assetclass."):
            s = s.split(".", 1)[1]
        return s

    def _emit(self, decision: "Decision", *, asset_class, decisions: list) -> None:
        """Append a decision to the in-memory list AND persist it to the
        DecisionStore when one is configured. Audit metadata is filled in
        from orchestrator state if the caller didn't provide it."""
        d = self._attach_audit(decision)
        decisions.append(d)
        if self._decision_store is None:
            return
        try:
            self._decision_store.append(
                d,
                strategy=self._strategy_name(),
                regime=self._regime,
                asset_class=self._normalise_asset_class(asset_class),
            )
        except Exception:
            # Decision persistence failures must never block trading. The in-
            # memory ScanResult is still returned, the daemon's runs/ JSON
            # logs still capture the event, and the supervisor email path is
            # unaffected. A persistence outage is observability noise, not a
            # trading blocker.
            pass

    def _build_state(self) -> RiskState:
        if self._state_builder is not None:
            return self._state_builder()
        # Bucket A: when no live builder is wired (test harnesses, dry-runs),
        # still surface halted_strategies from the on-disk file so per-lane
        # operator pauses are honored even on fallback. size_multiplier=1.0
        # is the safe default; the Pnl-driven throttle only applies when the
        # real builder is in play.
        # Bucket E: log a warning on this path. Pre-Bucket-E it silently
        # bypassed every state-driven gate (P&L breach, halt) by returning
        # zeros, with no visibility. The log line means an operator running
        # the orchestrator without a state builder sees the gap immediately.
        import logging as _logging
        from trading_bot.state_pause import (
            HALTED_STRATEGIES_PATH,
            read_halted_strategies,
        )
        _logging.getLogger(__name__).warning(
            "TradeOrchestrator: no state_builder wired; using zero P&L "
            "fallback. Daily/weekly halt + throttle gates are inert. This "
            "is expected in dry-run / tests, but a live deployment must "
            "wire a PnlStateBuilder."
        )
        return RiskState(
            daily_pnl_pct=Decimal("0"),
            weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0,
            halted=False,
            halted_strategies=read_halted_strategies(HALTED_STRATEGIES_PATH),
        )

    def _maybe_run_risk_debate(self, *, order, sig, state, account) -> str | None:
        """Run the 3-way risk debate when the operational context is
        borderline. Returns a block-reason string if the judge says reject
        with high|medium confidence; None otherwise (including all
        fail-open paths)."""
        from trading_bot.risk_debate import run_risk_debate, should_debate
        from trading_bot.decision_lessons import recent_lessons_text

        try:
            trade_var: float | None = None
            if account is not None and account.equity and account.equity > 0:
                risk_dollars = (sig.entry_price - sig.stop_loss_price) * sig.qty
                trade_var = float(risk_dollars / account.equity)
            if not should_debate(
                consecutive_losing_days=state.consecutive_losing_days,
                size_multiplier=getattr(state, "size_multiplier", None),
                trade_var=trade_var,
            ):
                return None
            try:
                lessons_block = recent_lessons_text(
                    self._risk_debate_engine,
                    symbol=order.symbol,
                    strategy=self._strategy_name(),
                    n_focused=4, n_cross=3,
                )
            except Exception:
                lessons_block = ""
            verdict = run_risk_debate(
                self._risk_debate_engine,
                symbol=order.symbol,
                action="buy",
                qty=sig.qty,
                entry_price=sig.entry_price,
                stop_loss_price=sig.stop_loss_price,
                strategy=self._strategy_name(),
                regime=self._regime,
                consecutive_losing_days=state.consecutive_losing_days,
                size_multiplier=getattr(state, "size_multiplier", None),
                trade_var=trade_var,
                lessons_block=lessons_block,
            )
        except Exception:
            return None  # any error in the debate path → fail-open
        if verdict is None:
            return None
        if verdict.recommendation == "reject" and verdict.confidence in ("high", "medium"):
            return f"risk_debate({verdict.confidence}): {verdict.reason}"
        return None

    def _maybe_run_unblock_debate(
        self, *, order, sig, state, account,
        rule: str, detail: str, asset_class_label: str,
    ) -> bool:
        """Phase 5.5/5.6 — when a deterministic risk gate REJECTS a trade,
        give the unblock committee a chance to override. Returns True if
        the gate should be overridden (proceed to place); False to respect
        the rejection (default).

        Default-deny: any error / disabled flag / missing engine / failed
        predicate / committee 'reject' / fail-closed verdict → returns
        False so the existing rejection behavior is preserved. The
        committee never invents trades; it only re-evaluates ones the
        deterministic logic already considered.
        """
        if not self._unblock_debate_enabled:
            return False
        if self._unblock_debate_engine is None:
            return False

        try:
            from trading_bot.unblock_debate import (
                run_unblock_debate, should_unblock_debate,
            )
            from trading_bot.options.wheel_runner import _count_todays_unblock_debates

            # Build the brief — orchestrator-flavored, slightly different
            # shape from the wheel-flavored brief but same prompt structure.
            equity_d = account.equity if account is not None else None
            risk_dollars = (sig.entry_price - sig.stop_loss_price) * sig.qty

            # Heuristic candidate score: signal-strength proxy. We don't
            # have IV-rank for equities; use sentiment + regime alignment
            # as the conviction proxy. Range 0-10.
            sentiment = float(getattr(sig, "sentiment_score", 0.0) or 0.0)
            sentiment_component = max(0.0, min(5.0, (sentiment + 1.0) * 2.5))
            # 5pt for "the signal exists at all" — this is a borderline
            # rejection on a candidate the strategy *did* select.
            score = round(5.0 + sentiment_component, 2)

            # Overage ratio is hard to compute generically across all
            # rule kinds (per_trade_risk_pct vs daily_loss vs sector_cap
            # all have different mechanics). Use a conservative default
            # that admits any non-extreme rejection: 0.30 (within 50% cap).
            overage_ratio = 0.30

            daily_count = _count_todays_unblock_debates(self._unblock_debate_engine)
            wheel_cfg = getattr(self._cfg, "wheel", None)
            # Adaptive thresholds: consult the overrides table FIRST. Fall
            # back to static YAML when no fresh override exists. Same shape
            # as the wheel-runner-side predicate so the two paths stay in
            # lockstep.
            try:
                from trading_bot.threshold_overrides import lookup as _lookup_override
                ov_min = _lookup_override(self._unblock_debate_engine,
                                          knob="unblock_min_candidate_score")
                ov_max = _lookup_override(self._unblock_debate_engine,
                                          knob="unblock_max_overage_ratio")
                ov_cap = _lookup_override(self._unblock_debate_engine,
                                          knob="unblock_daily_debate_cap")
            except Exception:
                ov_min = ov_max = ov_cap = None
            min_score = float(ov_min) if ov_min is not None else float(
                getattr(wheel_cfg, "unblock_min_candidate_score", 7.0))
            max_overage = float(ov_max) if ov_max is not None else float(
                getattr(wheel_cfg, "unblock_max_overage_ratio", 0.50))
            daily_cap = int(ov_cap) if ov_cap is not None else int(
                getattr(wheel_cfg, "unblock_daily_debate_cap", 15))

            if not should_unblock_debate(
                rejection_reason=f"{rule}: {detail}",
                rejection_overage_ratio=overage_ratio,
                candidate_score=score,
                daily_debate_count=daily_count,
                max_overage_ratio=max_overage,
                min_score=min_score,
                daily_cap=daily_cap,
            ):
                return False

            proposal = (
                f"  symbol:           {order.symbol}\n"
                f"  action:           buy ({asset_class_label})\n"
                f"  qty:              {sig.qty}\n"
                f"  entry_price:      {sig.entry_price}\n"
                f"  stop_loss_price:  {sig.stop_loss_price}\n"
                f"  per_trade_risk:   {risk_dollars}\n"
                f"  signal_reason:    {getattr(sig, 'reason', '?')}\n"
            )
            fundamentals = (
                f"  sentiment:        {sentiment}\n"
                f"  regime:           {self._regime}\n"
                f"  candidate_score:  {score} (0-10)\n"
                f"  strategy:         {self._strategy_name()}\n"
            )
            operational_context = (
                f"  equity_usd:               {equity_d}\n"
                f"  consecutive_losing_days:  {state.consecutive_losing_days}\n"
                f"  size_multiplier:          {getattr(state, 'size_multiplier', None)}\n"
            )

            verdict = run_unblock_debate(
                self._unblock_debate_engine,
                proposal_summary=proposal,
                block_reason=f"{rule}: {detail}",
                overage_ratio=overage_ratio,
                fundamentals=fundamentals,
                operational_context=operational_context,
                use_mailbox=False,  # synchronous in-loop scan
            )
        except Exception:
            return False  # any error in the debate path → respect the gate

        # Persist + email regardless of verdict.
        try:
            from sqlalchemy.orm import Session
            from trading_bot.state_db import UnblockDebateRun
            with Session(self._unblock_debate_engine) as s:
                row = UnblockDebateRun(
                    run_at=datetime.now(timezone.utc),
                    asset_class=asset_class_label,
                    symbol=order.symbol,
                    candidate_score=score,
                    block_reason=f"{rule}: {detail}",
                    overage_ratio=overage_ratio,
                    verdict=(verdict.recommendation if verdict else "fail_closed"),
                    confidence=(verdict.confidence if verdict else "low"),
                    judge_reason=(verdict.reason if verdict else "no verdict (fail-closed)"),
                    aggressive_text=(verdict.aggressive_text if verdict else ""),
                    conservative_text=(verdict.conservative_text if verdict else ""),
                    neutral_text=(verdict.neutral_text if verdict else ""),
                    synthetic=False,
                )
                s.add(row)
                s.commit()
        except Exception:
            pass

        # Real-time bus emit (Phase 2). One event per debate so the
        # _activity_feed and Unblock Debate node can light up
        # immediately. Critical-tier: never dropped under backpressure.
        try:
            from trading_bot.event_bus import bus as _bus
            _bus.emit(
                "debate.unblock.completed",
                {
                    "asset_class": asset_class_label,
                    "symbol": order.symbol,
                    "verdict": (verdict.recommendation if verdict else "fail_closed"),
                    "confidence": (verdict.confidence if verdict else "low"),
                    "block_reason": f"{rule}: {detail}",
                    "candidate_score": score,
                    "overage_ratio": overage_ratio,
                },
                source="orchestrator.unblock_debate",
            )
        except Exception:
            pass

        try:
            from trading_bot.email_unblock_debate import (
                DebateEmailContext, send_debate_email,
            )
            send_debate_email(DebateEmailContext(
                asset_class=asset_class_label, symbol=order.symbol,
                block_reason=f"{rule}: {detail}",
                overage_ratio=overage_ratio,
                candidate_score=score,
                proposal_summary=proposal,
                fundamentals=fundamentals,
                operational_context=operational_context,
                verdict=verdict,
            ))
        except Exception:
            pass

        if verdict is None:
            return False
        return verdict.recommendation == "place"

    def _resolve_strategy_for_ticker(
        self, *, symbol: str, asset_class: str,
    ) -> tuple[object | None, float | None]:
        """Pick the strategy for one ticker. Returns (strategy, intel_score).

        The orchestrator-level ``self._strategy`` already encodes the
        regime-default. When that's set, use it (existing behavior).
        When it's None (sideways / trending_down / risk_off), look up the
        ticker's intel score and re-resolve via ``strategy_for_regime``
        with the score; that may unlock Momentum for high-intel tickers
        in non-trending regimes (risk_off still returns None).

        Returns ``(None, intel_score)`` when no strategy is enabled.
        """
        # Look up intel score regardless of orchestrator-level strategy so
        # the audit trail captures it for every ticker, not just overrides.
        intel_score: float | None = None
        if self._intel_lookup_engine is not None:
            try:
                from trading_bot.intel.pool import lookup_score
                intel_score = lookup_score(
                    self._intel_lookup_engine, symbol, asset_class,
                )
            except Exception:
                intel_score = None

        if self._strategy is not None:
            return self._strategy, intel_score

        # Per-ticker re-resolution path.
        try:
            from trading_bot.strategy import strategy_for_regime
            ticker_strategy = strategy_for_regime(
                self._regime,
                intel_score=intel_score,
                intel_score_threshold=self._intel_score_regime_override_threshold,
            )
        except Exception:
            ticker_strategy = None
        return ticker_strategy, intel_score

    def _run_entry_debate(
        self, *, symbol: str, asset_class_label: str,
        sig, intel_score: float | None, indicators_text: str,
        operational_context: str, account, proposal_summary: str,
    ):
        """Fire the pre-trade entry committee. Returns the verdict (or
        ``None`` on any error → caller treats None as fail-soft skip).

        Persists every debate (regardless of verdict) to ``entry_debate_runs``
        and emits ``"debate.entry.completed"`` on the event bus. Same
        bookkeeping pattern as ``_maybe_run_unblock_debate``. The
        ``proposal_summary`` text is passed in (instead of built here) so the
        caller can reuse the same string when sending the outcome email.
        """
        from trading_bot.entry_debate import (
            count_todays_entry_debates, run_entry_debate, should_entry_debate,
        )
        if not self._entry_debate_enabled:
            return None
        if self._entry_debate_engine is None:
            return None
        try:
            daily_count = count_todays_entry_debates(self._entry_debate_engine)
            if not should_entry_debate(
                daily_debate_count=daily_count,
                daily_cap=self._entry_debate_daily_cap,
            ):
                return ("over_cap", None)
            proposal = proposal_summary
            top_reason = ""
            if self._intel_lookup_engine is not None:
                try:
                    from trading_bot.intel.pool import lookup as _intel_lookup
                    pe = _intel_lookup(self._intel_lookup_engine, symbol, asset_class_label)
                    if pe is not None:
                        top_reason = pe.top_reason
                except Exception:
                    pass
            # Phase D — inject the latest lessons block (if fresh) so the
            # entry debate's reasoning sees recent realised outcomes.
            try:
                from trading_bot.lesson_loop import latest_lesson_block
                lessons_block = latest_lesson_block(self._entry_debate_engine)
            except Exception:
                lessons_block = ""
            verdict = run_entry_debate(
                self._entry_debate_engine,
                proposal_summary=proposal,
                intel_score=intel_score,
                intel_top_reason=top_reason,
                signal_reason=getattr(sig, "reason", "") or "",
                regime=self._regime,
                indicators=indicators_text,
                operational_context=operational_context,
                lessons_block=lessons_block,
                use_mailbox=True,
            )
        except Exception:
            verdict = None  # any setup error → fail-soft

        # Persist (even when verdict is None — the audit row records the failure).
        try:
            from sqlalchemy.orm import Session
            from trading_bot.state_db import EntryDebateRun
            with Session(self._entry_debate_engine) as s:
                row = EntryDebateRun(
                    run_at=datetime.now(timezone.utc),
                    asset_class=asset_class_label,
                    symbol=symbol,
                    intel_score=intel_score,
                    signal_reason=getattr(sig, "reason", "") or "",
                    regime=self._regime,
                    verdict=(verdict.recommendation if verdict else "fail_soft"),
                    confidence=(verdict.confidence if verdict else "low"),
                    judge_reason=(verdict.reason if verdict else "no verdict (fail-soft)"),
                    aggressive_text=(verdict.aggressive_text if verdict else ""),
                    conservative_text=(verdict.conservative_text if verdict else ""),
                    neutral_text=(verdict.neutral_text if verdict else ""),
                    synthetic=False,
                )
                s.add(row)
                s.commit()
        except Exception:
            pass

        # Real-time bus emit so the dashboard's Entry Debate node lights up.
        try:
            from trading_bot.event_bus import bus as _bus
            _bus.emit(
                "debate.entry.completed",
                {
                    "asset_class": asset_class_label,
                    "symbol": symbol,
                    "verdict": (verdict.recommendation if verdict else "fail_soft"),
                    "confidence": (verdict.confidence if verdict else "low"),
                    "intel_score": intel_score,
                    "regime": self._regime,
                },
                source="orchestrator.entry_debate",
            )
        except Exception:
            pass

        return verdict

    def _send_entry_debate_email_safe(
        self, *, verdict, outcome: str, symbol: str,
        asset_class_label: str, intel_score: float | None,
        signal_reason: str, regime: str, proposal_summary: str,
        indicators: str, operational_context: str,
        entry_order_id: str = "", place_error: str = "",
    ) -> None:
        """Send the entry-debate transcript email. Wrapped in try/except —
        an SMTP failure must never crash the scan or block other tickers."""
        try:
            from trading_bot.email_entry_debate import (
                EntryDebateEmailContext, send_entry_debate_email,
            )
            ctx = EntryDebateEmailContext(
                asset_class=asset_class_label, symbol=symbol,
                intel_score=intel_score, signal_reason=signal_reason,
                regime=regime, proposal_summary=proposal_summary,
                indicators=indicators, operational_context=operational_context,
                verdict=verdict, outcome=outcome,
                entry_order_id=entry_order_id, place_error=place_error,
            )
            send_entry_debate_email(ctx)
        except Exception:
            pass

    def _news_intel_gate(self, symbol: str, asset_class: str) -> str | None:
        """Per-trade news/intel gates. Returns skip reason or None.

        Each gate is independently config-flagged and source-failure-safe
        (a network error returns None — never blocks trading on failed intel).
        """
        from trading_bot.intel_gates import (
            stock_earnings_gate, crypto_fear_greed_gate,
            crypto_reddit_spike_gate, macro_shock_gate,
            stock_insider_cluster_gate, crypto_coingecko_gate,
        )
        cfg = self._cfg.strategy

        # Macro shock applies to ALL asset classes
        if getattr(cfg, "macro_shock_gate_enabled", False):
            r = macro_shock_gate(threshold=cfg.macro_shock_threshold)
            if r is not None:
                return r

        if asset_class == "crypto":
            if getattr(cfg, "crypto_fear_greed_enabled", False):
                r = crypto_fear_greed_gate(
                    floor=cfg.crypto_fear_greed_floor,
                    ceiling=cfg.crypto_fear_greed_ceiling,
                )
                if r is not None:
                    return r
            if getattr(cfg, "crypto_reddit_spike_enabled", False):
                r = crypto_reddit_spike_gate(
                    symbol, multiplier=cfg.crypto_reddit_spike_multiplier,
                )
                if r is not None:
                    return r
            if getattr(cfg, "crypto_coingecko_enabled", False):
                r = crypto_coingecko_gate(
                    symbol, sentiment_floor=cfg.crypto_coingecko_sentiment_floor,
                )
                if r is not None:
                    return r
            return None

        # stock asset class
        if getattr(cfg, "earnings_gate_enabled", False):
            r = stock_earnings_gate(
                symbol, lookahead_days=cfg.earnings_gate_lookahead_days,
            )
            if r is not None:
                return r
        if getattr(cfg, "insider_cluster_enabled", False):
            r = stock_insider_cluster_gate(
                symbol, sell_volume_threshold=cfg.insider_cluster_threshold,
            )
            if r is not None:
                return r
        return None

    def scan(self, *, watchlist: list[WatchlistEntry]) -> ScanResult:
        account = self._alpaca.get_account()
        positions = self._alpaca.get_positions()
        try:
            open_order_symbols = self._alpaca.get_open_order_symbols()
        except AlpacaClientError:
            open_order_symbols = set()
        state = self._build_state()
        decisions: list[Decision] = []

        # Phase 6: removed unconditional early-bail when self._strategy is
        # None. The per-ticker loop now does intel-aware re-resolution so
        # a single high-intel ticker can still trade in sideways /
        # trending_down even when the orchestrator-level regime returned
        # no strategy. risk_off remains a hard wall (see strategy_for_regime).
        # Pre-Phase-6 short-circuit kept here only for risk_off so the
        # zero-trade case stays cheap (no per-ticker bar fetches).
        if self._strategy is None and self._regime == "risk_off":
            for entry in watchlist:
                self._emit(
                    Decision(
                        symbol=entry.symbol, action="hold",
                        reason=f"no strategy enabled for regime {self._regime}",
                    ),
                    asset_class=entry.asset_class,
                    decisions=decisions,
                )
            return ScanResult(decisions=decisions, timestamp=datetime.now(timezone.utc))

        # Defence-in-depth: build the set of symbols already entered today once
        # (outside the per-symbol loop) so the check is O(1) per symbol.
        traded_today = self._journal.traded_today()

        # W2b — load the restricted list once per scan; cheap (small YAML).
        restricted = self._load_restricted_list()
        # W2b — check the venue once per scan (orchestrator-level, not per
        # symbol). On rejection, every per-symbol decision below carries
        # approved_venue=False and we short-circuit with `escalate_to_human`.
        approved_venue_ok = True
        approved_venue_reason = ""
        if self._approved_venue_url is not None:
            from trading_bot.compliance import check_approved_venue
            approved_venue_ok, approved_venue_reason = check_approved_venue(
                self._approved_venue_url
            )
        if not approved_venue_ok:
            for entry in watchlist:
                self._emit(
                    Decision(
                        symbol=entry.symbol, action="escalate_to_human",
                        reason=f"approved_venue_check failed: {approved_venue_reason}",
                        compliance=ComplianceFlags(approved_venue=False),
                    ),
                    asset_class=entry.asset_class, decisions=decisions,
                )
            return ScanResult(decisions=decisions, timestamp=datetime.now(timezone.utc))

        # Phase F — circuit breaker check (orchestrator-level, before any
        # per-ticker work). When tripped, every entry is skipped with a
        # ``skipped_circuit_breaker`` decision. Open positions and the
        # hold-debate exit_now path are unaffected (we always preserve
        # the ability to cut losses, even when entries are frozen).
        breaker_state = None
        try:
            from trading_bot import circuit_breaker as _cb
            if self._intel_lookup_engine is not None:
                breaker_state = _cb.state(self._intel_lookup_engine)
        except Exception:
            breaker_state = None
        if breaker_state is not None and breaker_state.tripped:
            for entry in watchlist:
                self._emit(
                    Decision(
                        symbol=entry.symbol,
                        action="skipped_circuit_breaker",
                        reason=(
                            f"breaker tripped: {breaker_state.reason} "
                            f"(detail: {breaker_state.detail})"
                        ),
                    ),
                    asset_class=entry.asset_class, decisions=decisions,
                )
            return ScanResult(decisions=decisions, timestamp=datetime.now(timezone.utc))

        for entry in watchlist:
            symbol = entry.symbol
            ac = entry.asset_class
            # W2b — restricted list is the FIRST gate. A blocked symbol
            # never causes a market-data fetch or a strategy evaluation.
            if restricted:
                from trading_bot.compliance import check_restricted
                clear, reason = check_restricted(symbol, restricted=restricted)
                if not clear:
                    self._emit(
                        Decision(
                            symbol=symbol, action="skipped_restricted",
                            reason=reason,
                            compliance=ComplianceFlags(
                                approved_instrument=None,
                                approved_venue=True,
                                restricted_list_clear=False,
                                mnpi_clear=None,
                                market_abuse_clear=None,
                            ),
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
            if has_open_position(symbol, positions):
                self._emit(
                    Decision(symbol=symbol, action="skipped_existing_position"),
                    asset_class=ac, decisions=decisions,
                )
                continue
            # Also skip if there's already a pending open order for this symbol
            if symbol in open_order_symbols or symbol.replace("/", "") in open_order_symbols:
                self._emit(
                    Decision(symbol=symbol, action="skipped_pending_order"),
                    asset_class=ac, decisions=decisions,
                )
                continue
            # Last-resort idempotency: skip if this symbol was already bought today,
            # even if the position was subsequently stopped out.  Prevents the
            # "daemon restart catches up a missed fire → re-enters after stop" pattern
            # (root cause of the 2026-04-27 AMD/CLS/AMDL duplicate orders).
            if symbol in traded_today or symbol.replace("/", "") in traded_today:
                self._emit(
                    Decision(
                        symbol=symbol, action="skipped_already_traded_today",
                        reason="journal records a buy for this symbol today",
                    ),
                    asset_class=ac, decisions=decisions,
                )
                continue

            try:
                bars = self._market.get_daily_bars(symbol, lookback_days=60)
            except AlpacaClientError as e:
                self._emit(
                    Decision(symbol=symbol, action="api_error", reason=str(e)),
                    asset_class=ac, decisions=decisions,
                )
                continue

            if len(bars) < MIN_BARS_FOR_INDICATORS:
                self._emit(
                    Decision(
                        symbol=symbol, action="skipped_insufficient_data",
                        reason=f"{len(bars)} bars < {MIN_BARS_FOR_INDICATORS}",
                        data_quality=DataQualityFlags(
                            fresh=None, complete=False, aligned=None, provenance_ok=True,
                        ),
                    ),
                    asset_class=ac, decisions=decisions,
                )
                continue

            # ---- W2a Data-quality gates -----------------------------------
            # Freshness + completeness checks BEFORE strategy ingestion.
            # Fail-closed: a bad/stale source produces a skip with the
            # data_quality flags populated so the operator can see why.
            dq_cfg = getattr(self._cfg, "data_quality", None)
            if dq_cfg is not None and dq_cfg.enabled:
                from trading_bot.data_quality import (
                    check_bar_freshness, check_completeness,
                )
                max_age = (
                    dq_cfg.max_bar_age_hours_crypto
                    if entry.asset_class == "crypto"
                    else dq_cfg.max_bar_age_hours_stock
                )
                fresh_ok, fresh_reason = check_bar_freshness(
                    bars, asset_class=entry.asset_class,
                    max_age_hours=max_age,
                )
                complete_ok, complete_reason = check_completeness(
                    bars, max_missing_pct=dq_cfg.max_missing_ohlc_pct,
                )
                if not fresh_ok:
                    self._emit(
                        Decision(
                            symbol=symbol, action="skipped_stale_data",
                            reason=fresh_reason,
                            data_quality=DataQualityFlags(
                                fresh=False, complete=complete_ok,
                                aligned=None, provenance_ok=True,
                            ),
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
                if not complete_ok:
                    self._emit(
                        Decision(
                            symbol=symbol, action="skipped_incomplete_data",
                            reason=complete_reason,
                            data_quality=DataQualityFlags(
                                fresh=True, complete=False,
                                aligned=None, provenance_ok=True,
                            ),
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
                # Successful gate — record affirmative flags so downstream
                # decisions on this symbol carry truthful data_quality.
                _data_quality_pass = DataQualityFlags(
                    fresh=True, complete=True, aligned=True, provenance_ok=True,
                )
            else:
                _data_quality_pass = DataQualityFlags()

            ind = compute_indicators(bars)
            # Phase 6 — per-ticker strategy resolution: a high intel score
            # can unlock Momentum for sideways/trending_down even when the
            # orchestrator-level regime returned no strategy.
            ticker_strategy, ticker_intel_score = self._resolve_strategy_for_ticker(
                symbol=symbol, asset_class=ac,
            )
            if ticker_strategy is None:
                self._emit(
                    Decision(
                        symbol=symbol, action="hold",
                        reason=(
                            f"no strategy enabled for regime {self._regime}"
                            + (f" (intel_score={ticker_intel_score:.2f} below "
                               f"override threshold "
                               f"{self._intel_score_regime_override_threshold:.2f})"
                               if ticker_intel_score is not None else "")
                        ),
                    ),
                    asset_class=ac, decisions=decisions,
                )
                continue
            sig = ticker_strategy.evaluate(symbol, ind, equity=account.equity)
            if sig.action != SignalAction.BUY:
                self._emit(
                    Decision(symbol=symbol, action="hold", reason=sig.reason),
                    asset_class=ac, decisions=decisions,
                )
                continue

            # News-sentiment gate (Plan 6c). When sentiment_floor is set,
            # skip entries on names with recent-news score below floor.
            # Crypto bypasses (Massive's news endpoint is equity-focused).
            sf = getattr(self._cfg.strategy, "sentiment_floor", None)
            if sf is not None and entry.asset_class != "crypto":
                from trading_bot.news_sentiment import score_for, passes_filter
                score = score_for(
                    symbol,
                    max_age_days=self._cfg.strategy.sentiment_max_age_days,
                )
                if not passes_filter(score, floor=sf):
                    self._emit(
                        Decision(
                            symbol=symbol, action="skipped_sentiment",
                            reason=f"news score {score:.2f} < floor {sf:.2f}",
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue

            # ---- News/intel filter gates (each independently config-flagged)
            # All defensive: any source failure (network, API down, parse error)
            # falls through silently — never blocks trading on intel failure.
            try:
                skip_reason = self._news_intel_gate(symbol, entry.asset_class)
                if skip_reason is not None:
                    self._emit(
                        Decision(
                            symbol=symbol, action="skipped_intel",
                            reason=skip_reason,
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
            except Exception:
                pass  # any unexpected error: don't block trading

            asset_class = AssetClass.CRYPTO if entry.asset_class == "crypto" else AssetClass.STOCK
            order = OrderRequest(
                symbol=symbol,
                qty=sig.qty,
                side=OrderSide.BUY,
                asset_class=asset_class,
                limit_price=sig.entry_price,
                stop_loss_price=sig.stop_loss_price,
            )

            try:
                self._risk.check(order, account=account, positions=positions,
                                 state=state, regime=self._regime)
            except RiskRuleViolation as e:
                # Phase 5.5/5.6 — give the unblock committee a chance to
                # override on borderline rejections. Fail-CLOSED: any
                # error / disabled flag / rejected verdict → original
                # rejection stands.
                override = self._maybe_run_unblock_debate(
                    order=order, sig=sig, state=state, account=account,
                    rule=e.rule, detail=e.detail,
                    asset_class_label=entry.asset_class,
                )
                if not override:
                    self._emit(
                        Decision(
                            symbol=symbol, action="rejected_by_risk",
                            reason=f"{e.rule}: {e.detail}",
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
                # Override path — proceed past the gate. Note in the
                # decision audit so dashboard can see this came from a
                # committee override.
                self._emit(
                    Decision(
                        symbol=symbol, action="unblock_override",
                        reason=f"committee overrode {e.rule}: {e.detail}",
                    ),
                    asset_class=ac, decisions=decisions,
                )

            # Optional adversarial risk review. Fail-open: if LLM is
            # unavailable or the verdict is None, the trade proceeds as
            # before. Block only on high|medium-confidence reject.
            if self._risk_debate_enabled and self._risk_debate_engine is not None:
                debate_block_reason = self._maybe_run_risk_debate(
                    order=order, sig=sig, state=state, account=account,
                )
                if debate_block_reason is not None:
                    self._emit(
                        Decision(
                            symbol=symbol, action="rejected_by_risk_debate",
                            reason=debate_block_reason,
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue

            # Phase 6 — pre-trade entry committee. Fires on every BUY
            # signal that passed the deterministic risk gate. Fail-SOFT:
            # any failure (no creds, mailbox down, schema mismatch,
            # over-cap) skips the trade AND queues an alert so the
            # operator knows the LLM gate was unreachable.
            #
            # When the debate runs to completion (verdict in place/skip),
            # we email the operator a full transcript with the actual
            # outcome (placed / place_failed / skipped) — pre-built shared
            # context here so the email helper can be called from any of
            # the three outcome branches without rebuilding strings.
            entry_verdict_for_email = None  # populated when debate produced a verdict
            entry_email_kwargs: dict | None = None
            if self._entry_debate_enabled and self._entry_debate_engine is not None:
                indicators_text = (
                    f"  rsi_14:    {ind.rsi_14:.1f}\n"
                    f"  macd:      {ind.macd:.3f} (signal {ind.macd_signal:.3f})\n"
                    f"  ema_20:    {ind.ema_20:.2f}\n"
                    f"  last_close:{ind.last_close:.2f}\n"
                    f"  return_5d: {ind.return_5d:+.4f}\n"
                )
                operational_context = (
                    f"  equity_usd:               {account.equity}\n"
                    f"  consecutive_losing_days:  {state.consecutive_losing_days}\n"
                    f"  size_multiplier:          {getattr(state, 'size_multiplier', None)}\n"
                )
                risk_dollars = (sig.entry_price - sig.stop_loss_price) * sig.qty
                proposal_summary = (
                    f"  symbol:           {symbol}\n"
                    f"  asset_class:      {entry.asset_class}\n"
                    f"  qty:              {sig.qty}\n"
                    f"  entry_price:      {sig.entry_price}\n"
                    f"  stop_loss_price:  {sig.stop_loss_price}\n"
                    f"  per_trade_risk:   {risk_dollars}\n"
                )
                entry_verdict = self._run_entry_debate(
                    symbol=symbol, asset_class_label=entry.asset_class,
                    sig=sig, intel_score=ticker_intel_score,
                    indicators_text=indicators_text,
                    operational_context=operational_context, account=account,
                    proposal_summary=proposal_summary,
                )
                # Three outcomes:
                #   ("over_cap", None) → daily debate cap hit; skip with reason
                #   None              → fail-soft path; skip + queue alert
                #   verdict.recommendation in {"place","skip"} → act on it
                if isinstance(entry_verdict, tuple) and entry_verdict and entry_verdict[0] == "over_cap":
                    self._emit(
                        Decision(
                            symbol=symbol,
                            action="skipped_entry_debate_over_cap",
                            reason=(
                                f"daily entry-debate cap hit "
                                f"({self._entry_debate_daily_cap}); "
                                "raise cap or tighten upstream gates"
                            ),
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
                if entry_verdict is None:
                    # Fail-soft: queue an operator alert so the LLM-gate
                    # outage is visible, then skip the trade. No email —
                    # there's no transcript to send when the debate didn't
                    # produce a verdict; the alert covers operator visibility.
                    try:
                        from trading_bot.alerts import AlertEvent, queue_alert
                        queue_alert(AlertEvent(
                            kind="daemon_critical",
                            severity="warn",
                            title=f"Entry debate unreachable for {symbol} — trade skipped",
                            detail_html=(
                                f"<p>Entry debate returned no verdict for "
                                f"<code>{symbol}</code> ({entry.asset_class}). "
                                "The trade was NOT placed (fail-soft).</p>"
                                "<p>Likely causes: missing ANTHROPIC_API_KEY, "
                                "mailbox queue down, monthly LLM budget halted, "
                                "or transient SDK error. See logs for details.</p>"
                            ),
                            fired_at=datetime.now(timezone.utc),
                            dedup_key=f"entry_debate_unreachable:{datetime.now(timezone.utc).date().isoformat()}",
                        ))
                    except Exception:
                        pass
                    self._emit(
                        Decision(
                            symbol=symbol,
                            action="skipped_entry_debate_unreachable",
                            reason="entry debate returned no verdict (fail-soft)",
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
                # The debate produced a verdict. Stash everything the
                # outcome-email needs so the call sites below stay short.
                entry_verdict_for_email = entry_verdict
                entry_email_kwargs = dict(
                    verdict=entry_verdict,
                    symbol=symbol,
                    asset_class_label=entry.asset_class,
                    intel_score=ticker_intel_score,
                    signal_reason=getattr(sig, "reason", "") or "",
                    regime=self._regime,
                    proposal_summary=proposal_summary,
                    indicators=indicators_text,
                    operational_context=operational_context,
                )
                if entry_verdict.recommendation == "skip":
                    self._send_entry_debate_email_safe(
                        outcome="skipped", **entry_email_kwargs,
                    )
                    self._emit(
                        Decision(
                            symbol=symbol,
                            action="rejected_by_entry_debate",
                            reason=f"entry_debate({entry_verdict.confidence}): {entry_verdict.reason}",
                        ),
                        asset_class=ac, decisions=decisions,
                    )
                    continue
                # verdict.recommendation == "place" → fall through to
                # place_order_with_stop_loss below; email is sent there
                # so we know whether the broker accepted the order.

            try:
                result = self._alpaca.place_order_with_stop_loss(order)
            except AlpacaClientError as e:
                # If the entry debate said "place" but Alpaca rejected the
                # order, the operator must see both: judge said yes, broker
                # said no. Email outcome = place_failed with the broker error.
                if entry_verdict_for_email is not None and entry_email_kwargs is not None:
                    self._send_entry_debate_email_safe(
                        outcome="place_failed", place_error=str(e),
                        **entry_email_kwargs,
                    )
                self._emit(
                    Decision(symbol=symbol, action="api_error", reason=str(e)),
                    asset_class=ac, decisions=decisions,
                )
                continue
            # Order placed successfully. If a debate ran (and said place),
            # send the success email with the entry_order_id back-filled.
            if entry_verdict_for_email is not None and entry_email_kwargs is not None:
                self._send_entry_debate_email_safe(
                    outcome="placed",
                    entry_order_id=getattr(result, "entry_order_id", "") or "",
                    **entry_email_kwargs,
                )

            self._journal.append(TradeRecord(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                side="buy",
                qty=sig.qty,
                price=sig.entry_price,
                asset_class=asset_class.value,
                strategy="momentum",
                regime=self._regime,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
                notes=sig.reason,
            ))
            # Phase C — capture entry-time intel snapshot so the hold
            # debate has a stable baseline to compare against. Best-effort:
            # snapshot failure must not undo the order.
            try:
                from trading_bot.hold_debate import write_intel_snapshot
                from trading_bot.intel.pool import lookup as _pool_lookup
                # Pull the intel pool entry to get top_reason + sources mix
                pool_entry = None
                state_engine = self._intel_lookup_engine
                if state_engine is not None:
                    try:
                        pool_entry = _pool_lookup(state_engine, symbol, ac)
                    except Exception:
                        pool_entry = None
                top_sources = (
                    list(pool_entry.sources.keys())
                    if pool_entry is not None and pool_entry.sources
                    else []
                )
                top_reason = pool_entry.top_reason if pool_entry is not None else ""
                sentiment_avg = (
                    pool_entry.sentiment_avg if pool_entry is not None else None
                )
                if state_engine is not None:
                    write_intel_snapshot(
                        state_engine,
                        entry_order_id=getattr(result, "entry_order_id", "") or "",
                        symbol=symbol,
                        asset_class=entry.asset_class,
                        entry_intel_score=ticker_intel_score,
                        entry_top_reason=top_reason,
                        entry_sentiment_avg=sentiment_avg,
                        entry_top_sources=top_sources,
                    )
            except Exception:
                pass
            # W2c — populate risk_after so audit logs carry post-trade
            # gross/net + a per-trade VaR contribution. Cheap, no DB hit.
            try:
                gross_after = sum(
                    (abs(p.market_value) for p in positions), Decimal("0")
                ) + (sig.entry_price * sig.qty)
                net_after = sum(
                    (p.market_value for p in positions), Decimal("0")
                ) + (sig.entry_price * sig.qty)
                trade_risk_dollars = (sig.entry_price - sig.stop_loss_price) * sig.qty
                _risk_after = RiskAfter(
                    trade_var=(
                        (trade_risk_dollars / account.equity)
                        if account.equity > 0 else None
                    ),
                    gross_after=(
                        (gross_after / account.equity)
                        if account.equity > 0 else None
                    ),
                    net_after=(
                        (net_after / account.equity)
                        if account.equity > 0 else None
                    ),
                )
            except Exception:
                _risk_after = RiskAfter()

            self._emit(
                Decision(
                    symbol=symbol, action="placed_order", reason=sig.reason,
                    entry_order_id=result.entry_order_id,
                    stop_loss_order_id=result.stop_loss_order_id,
                    data_quality=_data_quality_pass,
                    risk_after=_risk_after,
                    compliance=ComplianceFlags(
                        approved_instrument=True,  # in watchlist
                        approved_venue=approved_venue_ok,
                        restricted_list_clear=True,
                        mnpi_clear=True,  # passed soft intel gates above
                        market_abuse_clear=True,
                    ),
                ),
                asset_class=ac, decisions=decisions,
            )

        return ScanResult(decisions=decisions, timestamp=datetime.now(timezone.utc))
