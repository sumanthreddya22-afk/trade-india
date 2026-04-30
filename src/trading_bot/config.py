# src/trading_bot/config.py
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_bot.exceptions import ConfigError, LiveModeDisabled


class Settings(BaseSettings):
    """Secrets + mode pulled from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alpaca_api_key: str
    alpaca_api_secret: str
    alpaca_base_url: str = "https://paper-api.alpaca.markets/v2"
    gmail_user: str
    gmail_app_password: str
    bot_mode: Literal["paper", "live"] = "paper"
    # Optional: Massive (Polygon) API key for the full-market data layer
    # (universe screening, news sentiment, short interest). Commands that
    # need it fail fast with a clear error if missing.
    polygon_api_key: str = ""
    finnhub_api_key: str = ""

    @field_validator("bot_mode")
    @classmethod
    def enforce_paper_only(cls, v: str) -> str:
        if v != "paper":
            raise LiveModeDisabled()
        return v


class RiskConfig(BaseModel):
    daily_loss_limit_pct: float = Field(gt=0, le=10)
    weekly_loss_limit_pct: float = Field(gt=0, le=20)
    per_trade_risk_pct: float = Field(gt=0, le=5)
    max_position_pct: float = Field(gt=0, le=25)
    max_symbol_concentration_pct: float = Field(gt=0, le=25)
    max_consecutive_losing_days: int = Field(gt=0, le=10)
    unprotected_stop_pct: float = Field(default=0.05, gt=0, le=0.5)
    # Per-sector concentration cap (fraction of equity, e.g. 0.25 = 25%).
    # Applies across stocks + pending option collateral. Wheel CSPs reserve
    # collateral in their underlying's sector. 'Unknown'-classified symbols
    # never block. Set to 1.0 to disable the gate entirely.
    sector_cap_pct: float = Field(default=0.25, gt=0, le=1.0)
    # W2c — gross/net notional caps as % of equity.
    # Defaults are intentionally loose (paper-only); tighten when going live.
    gross_cap_pct: float = Field(default=200.0, gt=0, le=1000.0)
    net_cap_pct: float = Field(default=100.0, gt=0, le=500.0)


class AllocationConfig(BaseModel):
    """Bucket F: stocks_max_pct / crypto_max_pct / cash_floor_pct were
    dead config knobs (no enforcement code anywhere). Only options_max_pct
    is honored — the wheel collateral gate reads it. Pre-Bucket-F YAMLs
    set the dead keys; Pydantic v2's default extra=ignore makes those
    silently ignored, so keeping them here would only re-create the
    illusion that they do something.
    """
    options_max_pct: float = Field(ge=0, le=100)


class RegimeAllocation(BaseModel):
    stocks: float = Field(ge=0, le=100)
    crypto: float = Field(ge=0, le=100)
    options: float = Field(ge=0, le=100)
    cash: float = Field(ge=0, le=100)


class EmailConfig(BaseModel):
    """Bucket F: daily_summary_time_et / weekly_summary_day were dead config
    knobs. The 16:30 ET digest schedule is hardcoded in scheduler_jobs.py
    and there's no weekly digest job. ``to`` is the only field actually
    consumed (by EmailSender).
    """
    to: str


class StorageConfig(BaseModel):
    trade_journal_path: str


class RegimeConfig(BaseModel):
    """Regime-detection tuning knobs."""

    vol_threshold_pct: float = Field(default=22.0, gt=0, le=100)


class StrategyConfig(BaseModel):
    """Optional strategy-layer filters (Plan 6c+)."""

    # News-sentiment floor: if set, entries are skipped when the symbol's
    # most-recent cached sentiment score is below this. None = filter
    # disabled. Range -1..+1. Default disabled until backtest finds the
    # right value.
    sentiment_floor: float | None = Field(default=None, ge=-1.0, le=1.0)
    sentiment_max_age_days: int = Field(default=3, ge=1, le=30)
    # Earnings-window gate for momentum lane. When True, stock entries
    # are blocked if the symbol has earnings inside the next N trading
    # days. Crypto bypasses (no earnings). Filter-only — never opens new
    # positions, just refuses entries facing binary gap risk.
    earnings_gate_enabled: bool = Field(default=True)
    earnings_gate_lookahead_days: int = Field(default=5, ge=1, le=30)
    # Macro-shock gate (GDELT). When True, blocks all entries (stocks,
    # crypto, options) on days with extreme negative macro sentiment.
    macro_shock_gate_enabled: bool = Field(default=True)
    macro_shock_threshold: float = Field(default=-3.0, ge=-10.0, le=0.0)
    # Crypto Fear & Greed gate (Alternative.me). Blocks crypto entries
    # outside the [floor, ceiling] band of the index (0–100).
    crypto_fear_greed_enabled: bool = Field(default=True)
    crypto_fear_greed_floor: int = Field(default=20, ge=0, le=100)
    crypto_fear_greed_ceiling: int = Field(default=80, ge=0, le=100)
    # Crypto Reddit-mention spike gate (ApeWisdom r/CryptoCurrency).
    crypto_reddit_spike_enabled: bool = Field(default=True)
    crypto_reddit_spike_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    # CoinGecko per-coin community sentiment floor.
    crypto_coingecko_enabled: bool = Field(default=False)  # off by default — coin id mapping needed
    crypto_coingecko_sentiment_floor: float = Field(default=50.0, ge=0.0, le=100.0)
    # Insider-cluster signal for stocks (Finnhub). Default OFF —
    # big-cap execs sell on 10b5-1 schedules routinely (NVDA had 62 sells
    # in a 90d window in normal operation). The raw count is too noisy.
    # Operator can flip on and tune `insider_cluster_threshold` for narrow
    # use cases (e.g., small/mid-caps where 10+ sells in 90d is unusual).
    insider_cluster_enabled: bool = Field(default=False)
    insider_cluster_threshold: int = Field(default=20, ge=1, le=200)


class WheelConfig(BaseModel):
    enabled: bool = False
    delta_target_low: float = Field(default=0.20, ge=0.05, le=0.50)
    delta_target_high: float = Field(default=0.30, ge=0.05, le=0.50)
    dte_min: int = Field(default=30, ge=7, le=90)
    dte_max: int = Field(default=45, ge=7, le=90)
    take_profit_pct: float = Field(default=0.50, gt=0, lt=1)
    dte_force_close: int = Field(default=21, ge=1, le=45)
    delta_breach_csp: float = Field(default=0.45, gt=0, lt=1)
    delta_breach_cc: float = Field(default=0.55, gt=0, lt=1)
    max_rolls_per_cycle: int = Field(default=2, ge=0, le=5)
    iv_rank_floor: float = Field(default=30.0, ge=0, le=100)
    vix_floor: float = Field(default=15.0, ge=0, le=100)
    vix_ceiling: float = Field(default=30.0, ge=0, le=100)
    sentiment_floor: float = Field(default=-0.3, ge=-1, le=1)
    min_premium_abs: float = Field(default=0.20, ge=0)
    min_annualized_yield: float = Field(default=0.12, ge=0)
    min_open_interest: int = Field(default=100, ge=0)
    universe_cache_hours: int = Field(default=24, ge=1, le=168)
    blocklist_path: str = "strategy/wheel_blocklist.yaml"
    allowlist_path: str = "strategy/wheel_allowlist.yaml"


class DataQualityConfig(BaseModel):
    """W2a — pre-trade gates on bar quality. The orchestrator runs these
    between data fetch and indicator computation.  Defaults are tuned for
    daily bars; intraday paths can pass tighter values to ``check_bar_freshness``.
    """

    enabled: bool = Field(default=True)
    # Maximum age in hours for the most recent bar (per asset class).
    # Daily bars typically run within 48h during RTH (today's bar may not
    # be built until end-of-day). Crypto trades 24/7 so we expect fresh
    # bars every few hours.
    max_bar_age_hours_stock: float = Field(default=48.0, gt=0, le=168.0)
    max_bar_age_hours_crypto: float = Field(default=6.0, gt=0, le=168.0)
    # Maximum % of NaN values in OHLC columns before the gate trips.
    max_missing_ohlc_pct: float = Field(default=5.0, ge=0.0, le=50.0)


class AppConfig(BaseModel):
    risk: RiskConfig
    allocation: AllocationConfig
    regime_allocations: dict[str, RegimeAllocation]
    email: EmailConfig
    storage: StorageConfig
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    wheel: WheelConfig = Field(default_factory=WheelConfig)
    data_quality: DataQualityConfig = Field(default_factory=DataQualityConfig)


def load_config(path: Path) -> AppConfig:
    """Load YAML config from disk and validate."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    try:
        return AppConfig(**raw)
    except Exception as e:
        raise ConfigError(f"Config validation failed: {e}") from e
