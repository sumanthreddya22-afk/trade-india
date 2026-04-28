"""Role Protocol, dataclasses, and enums. The contract every routine
in the system implements. See spec §7 for the full role taxonomy.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class RoleStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    BLOCKED = "blocked"        # external dependency unavailable (creds, network)
    HALTED = "halted"          # internal fatal config bug; pause.flag written


class HealthStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"      # KPI worse than threshold or recent errors
    BLOCKED = "BLOCKED"        # cannot run currently (creds, upstream down)
    FAIL = "FAIL"              # consistently broken


@dataclass
class RoleResult:
    role_name: str
    started_at: dt.datetime
    finished_at: dt.datetime
    status: RoleStatus
    latency_ms: int
    outputs: dict[str, Any] = field(default_factory=dict)
    error_text: str | None = None


@dataclass
class ReportCard:
    role_name: str
    period_days: int
    kpi_name: str
    kpi_value: float
    summary: str
    delta_vs_prior: float | None = None
    health: HealthStatus = HealthStatus.OK


@dataclass
class Health:
    status: HealthStatus
    detail: str = ""


@runtime_checkable
class Role(Protocol):
    """Every routine in the system implements this Protocol."""
    name: str
    tier: int
    process: str            # "daemon" | "lab" | "supervisor"
    job_description: str
    sla_seconds: int
    upstream_roles: list[str]
    downstream_roles: list[str]

    def run(self, ctx: Any) -> RoleResult: ...
    def report_card(self, lookback_days: int) -> ReportCard: ...
    def health_check(self) -> Health: ...
