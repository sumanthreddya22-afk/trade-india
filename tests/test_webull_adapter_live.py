"""Live-smoke test for WebullAdapter — gated on ``WEBULL_LIVE_SMOKE=1``.

Per WS3 step 5: places a $1 BUY MKT in SIRI, verifies fill, cancels.
This test costs real money on a real Webull account — run only with
the operator's explicit knowledge during the shadow week.

To enable:
    WEBULL_LIVE_SMOKE=1 \\
    WEBULL_API_KEY=... WEBULL_API_SECRET=... WEBULL_ACCOUNT_ID=... \\
    ENABLE_SUBMIT=true \\
    uv run pytest tests/test_webull_adapter_live.py -v
"""
from __future__ import annotations

import os
import time

import pytest

LIVE = os.environ.get("WEBULL_LIVE_SMOKE", "").strip().lower() in ("1", "true", "yes")
pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="WEBULL_LIVE_SMOKE not set; refuses to place real orders.",
)


def test_live_dollar_smoke() -> None:
    from trading_bot.ingest.webull_adapter import WebullAdapter

    adapter = WebullAdapter()

    # Sanity: account fetch must succeed before we attempt to trade.
    acct = adapter.fetch_account()
    assert acct, "fetch_account returned empty — credentials or session bad"
    assert acct.get("equity", 0) > 5.0, "Account has < $5 equity — refuse"

    # Place a $1 BUY MKT in SIRI (cheap, liquid).
    coid = f"smoke_{int(time.time())}"
    r = adapter.submit_order(
        client_order_id=coid, symbol="SIRI", qty=1, side="buy",
        order_type="market", asset_class="us_equity",
    )
    assert r["ok"] is True, f"submit_order failed: {r}"
    broker_id = r["broker_order_id"]
    assert broker_id

    # Poll for terminal status.
    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        lookup = adapter.lookup_by_client_order_id(coid)
        if lookup and lookup.get("status") in ("filled", "canceled", "rejected"):
            final = lookup
            break
        time.sleep(1)
    assert final is not None, "Order did not reach terminal state in 30s"
    assert final["status"] == "filled", f"Order did not fill: {final}"

    # The fill exists. Verify position appears in fetch_positions briefly,
    # then exit. Note: exit cancel via a separate sell order.
    positions = adapter.fetch_positions()
    siri = [p for p in positions if p.get("symbol") == "SIRI"]
    assert siri, "SIRI position not reflected post-fill"

    # Sell back to flat.
    sell_coid = f"smoke_sell_{int(time.time())}"
    s = adapter.submit_order(
        client_order_id=sell_coid, symbol="SIRI", qty=1, side="sell",
        order_type="market", asset_class="us_equity",
    )
    assert s["ok"] is True, f"sell failed: {s}"
