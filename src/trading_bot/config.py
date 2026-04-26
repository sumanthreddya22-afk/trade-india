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


class AppConfig(BaseModel):
    risk: RiskConfig
    allocation: AllocationConfig
    regime_allocations: dict[str, RegimeAllocation]
    email: EmailConfig
    storage: StorageConfig
    regime: RegimeConfig = Field(default_factory=RegimeConfig)


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
