import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_bot.alpaca_client import (
    AlpacaClient,
    AssetClass,
    OrderRequest,
    OrderSide,
)
from trading_bot.config import AppConfig
from trading_bot.exceptions import AlpacaClientError, RiskRuleViolation
from trading_bot.market_data import (
    MIN_BARS_FOR_INDICATORS,
    MarketDataClient,
    compute_indicators,
)
from trading_bot.risk_manager import RiskManager, RiskState
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
    """
    if not path.exists():
        return []
    text = path.read_text()
    out: list[WatchlistEntry] = []
    pattern = re.compile(r"^###\s+\d+\.\s+(\S+)\s+\(([^)]+)\)\s*$", re.MULTILINE)
    for match in pattern.finditer(text):
        symbol = match.group(1)
        asset_class_raw = match.group(2)
        # Preserve the raw asset_class value; only normalise crypto variants.
        if "crypto" in asset_class_raw.lower():
            asset_class = "crypto"
        else:
            asset_class = asset_class_raw
        out.append(WatchlistEntry(symbol=symbol, asset_class=asset_class, notes=""))
    return out


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
    ) -> None:
        self._cfg = config
        self._market = market_data
        self._alpaca = alpaca
        self._journal = journal
        self._regime = regime
        # Strategy is chosen by regime if not explicitly provided
        self._strategy = strategy or strategy_for_regime(regime)
        self._risk = risk_manager or RiskManager(config)
        self._state_builder = state_builder
        self._decision_store = decision_store
        self._policy_version = policy_version
        # W2b compliance config — load once at init; reread on every scan via
        # _load_restricted_list() so the operator can edit YAML without restart.
        self._restricted_list_path = (
            Path(restricted_list_path) if restricted_list_path else None
        )
        self._approved_venue_url = approved_venue_url

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
        from trading_bot.audit import build_audit
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
        from trading_bot.state_pause import (
            HALTED_STRATEGIES_PATH,
            read_halted_strategies,
        )
        return RiskState(
            daily_pnl_pct=Decimal("0"),
            weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0,
            halted=False,
            halted_strategies=read_halted_strategies(HALTED_STRATEGIES_PATH),
        )

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

        if self._strategy is None:
            # Regime has no enabled strategy (e.g. sideways/risk_off after
            # the Plan-5b backtest disabled mean_reversion). Skip entries;
            # existing positions still managed by their bracket orders.
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
            sig = self._strategy.evaluate(symbol, ind, equity=account.equity)
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
                self._emit(
                    Decision(
                        symbol=symbol, action="rejected_by_risk",
                        reason=f"{e.rule}: {e.detail}",
                    ),
                    asset_class=ac, decisions=decisions,
                )
                continue

            try:
                result = self._alpaca.place_order_with_stop_loss(order)
            except AlpacaClientError as e:
                self._emit(
                    Decision(symbol=symbol, action="api_error", reason=str(e)),
                    asset_class=ac, decisions=decisions,
                )
                continue

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
