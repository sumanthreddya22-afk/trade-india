import json
import pytest
from trading_bot.cadence import CadenceConfig, load_cadence


def test_load_cadence_returns_dataclass_with_defaults(tmp_path):
    cfg_path = tmp_path / "paper_active.json"
    cfg_path.write_text(json.dumps({
        "version": "v1",
        "active_template": "momentum_v3",
        "params": {},
        "risk_caps": {"max_position_pct": 10, "daily_loss_pct": 3, "max_drawdown_pct": 20},
        "cadence": {
            "heartbeat_seconds": 60,
            "watchdog_seconds": 60,
            "account_sentinel_minutes_market": 5,
            "account_sentinel_minutes_offhours": 30,
            "schedule_auditor_minutes": 15,
            "resource_guardian_minutes": 30,
            "stock_scanner_minutes": 60,
            "crypto_scanner_minutes": 30,
            "portfolio_monitor_minutes": 60,
            "order_steward_sweep_minutes": 60,
            "vip_listener_minutes": 30,
            "sentiment_warm_times_et": ["08:55", "12:00"],
            "sentiment_stale_hours_for_on_demand": 4,
        },
    }))
    c = load_cadence(cfg_path)
    assert isinstance(c, CadenceConfig)
    assert c.heartbeat_seconds == 60
    assert c.crypto_scanner_minutes == 30
    assert c.sentiment_warm_times_et == ("08:55", "12:00")


def test_load_cadence_missing_block_uses_defaults(tmp_path):
    cfg_path = tmp_path / "paper_active.json"
    cfg_path.write_text(json.dumps({
        "version": "v1", "active_template": "momentum_v3", "params": {},
        "risk_caps": {"max_position_pct": 10, "daily_loss_pct": 3, "max_drawdown_pct": 20},
        # No cadence block
    }))
    c = load_cadence(cfg_path)
    # Defaults match the spec §9
    assert c.heartbeat_seconds == 60
    assert c.stock_scanner_minutes == 60


def test_cadence_config_is_frozen():
    c = CadenceConfig()
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        c.heartbeat_seconds = 120
