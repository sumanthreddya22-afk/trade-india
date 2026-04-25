class TradingBotError(Exception):
    """Base exception for all trading bot errors."""


class ConfigError(TradingBotError):
    """Raised when configuration is invalid or missing."""


class AlpacaClientError(TradingBotError):
    """Raised when an Alpaca API call fails."""


class LiveModeDisabled(TradingBotError):
    """Raised if anything attempts to enable live trading. This is paper-only."""

    def __init__(self) -> None:
        super().__init__(
            "Live trading is structurally disabled. This bot is paper-only. "
            "Live mode requires explicit code unlock and a separate authorization."
        )


class RiskRuleViolation(TradingBotError):
    """Raised when a trade violates a hard risk rule."""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"Risk rule violated: {rule} — {detail}")
