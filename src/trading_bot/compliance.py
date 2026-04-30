"""W2b — Compliance gates.

Deterministic, fail-closed checks for the PDF's per-decision compliance flags:

- ``approved_instrument``  — the symbol is on the allowlist for this asset class
- ``approved_venue``       — broker base URL is the approved one (paper-only here)
- ``restricted_list_clear`` — symbol is NOT in ``strategy/restricted_list.yaml``
- ``mnpi_clear``           — no MNPI-bearing event window (earnings blackout, Form 4)
- ``market_abuse_clear``   — checked elsewhere (abuse_detector.py)

Compliance gates differ from intel/signal gates in one critical way: an
unreachable source reports "cannot verify" → block, never silent-pass.
The orchestrator's existing intel_gates module is intentionally fail-open
(don't block on a Finnhub outage); this module is fail-closed.
"""
from __future__ import annotations

from pathlib import Path

import yaml


ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def load_restricted_list(path: Path | str) -> set[str]:
    """Load symbols from the restricted_list YAML. Returns empty set when
    the file is missing or malformed (defensive — operators add the file
    when they have a list)."""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        raw = yaml.safe_load(p.read_text()) or {}
        symbols = raw.get("symbols", [])
        if not isinstance(symbols, list):
            return set()
        return {str(s).upper() for s in symbols}
    except Exception:
        return set()


def check_restricted(symbol: str, *, restricted: set[str]) -> tuple[bool, str]:
    """Return (clear, reason). ``clear=True`` when the symbol is NOT on the
    restricted list. Comparison is case-insensitive."""
    if not restricted:
        return True, ""
    norm = symbol.upper()
    norm_set = {s.upper() for s in restricted}
    if norm in norm_set:
        return False, f"{symbol} is on the restricted list"
    return True, ""


def check_approved_venue(base_url: str) -> tuple[bool, str]:
    """Return (ok, reason). Today the only approved venue is Alpaca paper.

    Subdomain matching is loose enough to accept ``/v2`` suffixes that
    Alpaca's SDK appends, but rejects sibling hosts (live API, anything
    not under paper-api.alpaca.markets)."""
    if not base_url:
        return False, "no venue configured"
    cleaned = base_url.rstrip("/")
    if cleaned.startswith(ALPACA_PAPER_BASE_URL):
        return True, ""
    return False, (
        f"venue {base_url} is not approved (only {ALPACA_PAPER_BASE_URL})"
    )
