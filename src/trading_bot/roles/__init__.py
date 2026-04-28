"""Role definitions for the trading bot. See spec §7 for the taxonomy."""
from trading_bot.roles.base import (
    Health,
    HealthStatus,
    ReportCard,
    Role,
    RoleResult,
    RoleStatus,
)

__all__ = [
    "Role",
    "RoleResult",
    "ReportCard",
    "Health",
    "RoleStatus",
    "HealthStatus",
]
