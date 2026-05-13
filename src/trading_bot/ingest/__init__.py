"""v4 ingest layer (L1 + L1.5).

Phase 3 ships data freshness watermarks + corporate actions. L1 ingest
writers (alpaca_bars/quotes) and L1.5 alt-data sources land in their
respective phases.
"""
from __future__ import annotations

from trading_bot.ingest.corporate_actions import (
    CorporateAction, CrossCheckResult, apply_dividend_to_cash,
    apply_split_to_price, apply_split_to_qty, cross_check, record_action,
)
from trading_bot.ingest.schema import ensure_ingest_tables
from trading_bot.ingest.watermarks import (
    Watermark, check_lane_freshness, latest_watermark_for_lane,
    read_watermark, write_watermark,
)

__all__ = [
    "CorporateAction",
    "CrossCheckResult",
    "Watermark",
    "apply_dividend_to_cash",
    "apply_split_to_price",
    "apply_split_to_qty",
    "check_lane_freshness",
    "cross_check",
    "ensure_ingest_tables",
    "latest_watermark_for_lane",
    "read_watermark",
    "record_action",
    "write_watermark",
]
