#!/usr/bin/env python3
"""Refresh all cache-backed intel feeds.

Each feed that exposes ``refresh()`` is invoked once; failures are
logged but do not stop the script (other feeds still get their cache
updated). Designed to be run from cron / launchd / the daemon's
``job_intel_refresh`` every 6 hours.

Per-feed env opt-out: set ``TRADING_BOT_INTEL_DISABLE=feed_a,feed_b``
to skip specific feeds (useful when an API key is unavailable).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_bot.ingest.intel.cboe import CboeFeed  # noqa: E402
from trading_bot.ingest.intel.crypto_fear_greed import (  # noqa: E402
    CryptoFearGreedFeed,
)
from trading_bot.ingest.intel.treasury_yield_curve import (  # noqa: E402
    TreasuryYieldCurveFeed,
)

log = logging.getLogger("refresh_intel_caches")


def _build_registry() -> dict:
    fred_key = os.environ.get("TRADING_BOT_FRED_API_KEY")
    return {
        "treasury_yield_curve": TreasuryYieldCurveFeed(fred_api_key=fred_key),
        "crypto_fear_greed": CryptoFearGreedFeed(),
        "cboe": CboeFeed(),
    }


def refresh_all(*, only: tuple[str, ...] = ()) -> dict:
    """Refresh every registered feed. Returns a per-feed status dict.

    ``only`` restricts the refresh to specific feed_ids (useful for
    cron jobs that want to run different feeds on different cadences).
    """
    disabled = set(
        (os.environ.get("TRADING_BOT_INTEL_DISABLE", "") or "")
        .replace(" ", "").split(",")
    )
    out: dict[str, str] = {}
    for feed_id, feed in _build_registry().items():
        if only and feed_id not in only:
            continue
        if feed_id in disabled:
            out[feed_id] = "disabled"
            continue
        try:
            feed.refresh()
            out[feed_id] = "ok"
        except Exception as e:  # noqa: BLE001
            log.warning("refresh %s failed: %s", feed_id, e)
            out[feed_id] = f"fail: {type(e).__name__}: {e}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default="", help="comma-separated feed_ids to refresh")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    only = tuple(s.strip() for s in args.only.split(",") if s.strip())
    results = refresh_all(only=only)
    for feed_id, status in results.items():
        print(f"  {feed_id:<24s} {status}")
    failed = [k for k, v in results.items() if v.startswith("fail:")]
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
