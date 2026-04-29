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


class AllocationConfig(BaseModel):
    stocks_max_pct: float = Field(ge=0, le=100)
    crypto_max_pct: float = Field(ge=0, le=100)
    options_max_pct: float = Field(ge=0, le=100)
    cash_floor_pct: float = Field(ge=0, le=100)


class RegimeAllocation(BaseModel):
    stocks: float = Field(ge=0, le=100)
    crypto: float = Field(ge=0, le=100)
    options: float = Field(ge=0, le=100)
    cash: float = Field(ge=0, le=100)


class EmailConfig(BaseModel):
    to: str
    daily_summary_time_et: str
    weekly_summary_day: str


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
    wsb_spike_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    blocklist_path: str = "strategy/wheel_blocklist.yaml"
    allowlist_path: str = "strategy/wheel_allowlist.yaml"


class AppConfig(BaseModel):
    risk: RiskConfig
    allocation: AllocationConfig
    regime_allocations: dict[str, RegimeAllocation]
    email: EmailConfig
    storage: StorageConfig
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    wheel: WheelConfig = Field(default_factory=WheelConfig)


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
