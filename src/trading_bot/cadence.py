"""Reads the cadence: block from paper_active.json (or live_active.json).
Defaults match the values in spec §9. Frozen dataclass so callers can't
mutate it accidentally.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CadenceConfig:
    heartbeat_seconds: int = 60
    watchdog_seconds: int = 60
    account_sentinel_minutes_market: int = 5
    account_sentinel_minutes_offhours: int = 30
    schedule_auditor_minutes: int = 15
    resource_guardian_minutes: int = 30
    stock_scanner_minutes: int = 60
    crypto_scanner_minutes: int = 30
    portfolio_monitor_minutes: int = 60
    order_steward_sweep_minutes: int = 60
    vip_listener_minutes: int = 30
    sentiment_warm_times_et: tuple[str, ...] = ("08:55", "12:00")
    sentiment_stale_hours_for_on_demand: int = 4
    wheel_scan_enabled: bool = True
    wheel_manage_interval_minutes: int = 30


def load_cadence(path: str | Path) -> CadenceConfig:
    payload = json.loads(Path(path).read_text())
    block = payload.get("cadence", {})
    times = block.get("sentiment_warm_times_et")
    return CadenceConfig(
        heartbeat_seconds=block.get("heartbeat_seconds", 60),
        watchdog_seconds=block.get("watchdog_seconds", 60),
        account_sentinel_minutes_market=block.get("account_sentinel_minutes_market", 5),
        account_sentinel_minutes_offhours=block.get("account_sentinel_minutes_offhours", 30),
        schedule_auditor_minutes=block.get("schedule_auditor_minutes", 15),
        resource_guardian_minutes=block.get("resource_guardian_minutes", 30),
        stock_scanner_minutes=block.get("stock_scanner_minutes", 60),
        crypto_scanner_minutes=block.get("crypto_scanner_minutes", 30),
        portfolio_monitor_minutes=block.get("portfolio_monitor_minutes", 60),
        order_steward_sweep_minutes=block.get("order_steward_sweep_minutes", 60),
        vip_listener_minutes=block.get("vip_listener_minutes", 30),
        sentiment_warm_times_et=tuple(times) if times else ("08:55", "12:00"),
        sentiment_stale_hours_for_on_demand=block.get("sentiment_stale_hours_for_on_demand", 4),
        wheel_scan_enabled=block.get("wheel_scan_enabled", True),
        wheel_manage_interval_minutes=int(block.get("wheel_manage_interval_minutes", 30)),
    )
