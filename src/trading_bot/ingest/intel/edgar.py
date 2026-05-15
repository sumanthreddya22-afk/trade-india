"""SEC EDGAR intel feed — submission counts per entity.

EDGAR exposes a per-CIK submissions JSON document at
``https://data.sec.gov/submissions/CIK<10-digit>.json``. It is free,
unauthenticated, but requires an identifying ``User-Agent`` header.
A 10-digit zero-padded CIK is the canonical key.

Feed shape per series: the series_id is a human-friendly ticker tag
(e.g. ``"SPY_8K_7D"``). The value is the number of recent filings
of the configured form types within the lookback window.

Snapshot-only for now — Plan v4 §6 reserves news/catalyst gating
for after MVP-OP graduation. Today the value flows into
``feature_snapshot.intel_json`` so a backtest replay sees the same
catalyst pressure the live decision saw.

Network failures + parse errors raise ``IntelUnavailable``, in
keeping with the rest of the intel layer's fail-closed contract.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from trading_bot.ingest.intel.base import IntelRecord, IntelUnavailable

log = logging.getLogger(__name__)

# Default tickers + their CIKs. ETF trust CIKs — useful as
# structural-event canaries (manager changes, creation/redemption
# disclosures, etc.). Operator can extend.
DEFAULT_ENTITIES: dict[str, str] = {
    # ticker -> CIK (10-digit zero-padded)
    "SPY": "0000884394",   # SPDR S&P 500 ETF Trust
    "TLT": "0001100663",   # iShares 20+ Year Treasury Bond ETF
}

DEFAULT_FORMS: tuple[str, ...] = ("8-K", "8-K/A")
DEFAULT_LOOKBACK_DAYS = 7


@dataclass(frozen=True)
class EdgarFeed:
    """SEC EDGAR submissions feed. One HTTP call per entity per fetch.

    ``user_agent`` is REQUIRED by SEC fair-access rules. Operator
    should configure it as e.g. ``"trading-bot v4 contact@example.com"``
    so SEC can reach the operator if the bot misbehaves. The constructor
    accepts it as a kwarg or reads ``SEC_USER_AGENT`` from the env.
    """

    entities: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ENTITIES))
    forms: Sequence[str] = DEFAULT_FORMS
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    user_agent: str = ""
    base_url: str = "https://data.sec.gov/submissions"
    timeout_seconds: float = 10.0
    feed_id: str = "edgar_v1"

    def __post_init__(self) -> None:
        if not self.user_agent:
            import os
            ua = os.environ.get("SEC_USER_AGENT", "").strip()
            if ua:
                object.__setattr__(self, "user_agent", ua)
        # No user_agent → fetch will fail; surface as IntelUnavailable
        # at call time rather than at construction so dev-mode tests
        # that mock urlopen don't need to set the env.

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        if not self.user_agent:
            raise IntelUnavailable(
                "edgar: missing User-Agent — set SEC_USER_AGENT in env "
                "or pass user_agent= to EdgarFeed"
            )
        out: dict[str, IntelRecord] = {}
        fetched_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        cutoff = decision_date - dt.timedelta(days=int(self.lookback_days))
        forms_set = {f.upper() for f in self.forms}
        for ticker, cik in self.entities.items():
            try:
                count, latest_ts = self._count_recent(
                    cik=cik, forms=forms_set,
                    cutoff=cutoff, on_or_before=decision_date,
                )
            except IntelUnavailable:
                raise
            except Exception as e:  # noqa: BLE001
                raise IntelUnavailable(
                    f"edgar[{ticker}] fetch failed: {type(e).__name__}: {e}"
                ) from e
            forms_tag = "_".join(sorted(f.replace("-", "").replace("/", "")
                                         for f in forms_set))
            series_id = f"{ticker}_{forms_tag}_{int(self.lookback_days)}D"
            out[series_id] = IntelRecord(
                feed_id=self.feed_id,
                series_id=series_id,
                value=float(count),
                unit="count",
                source_ts=latest_ts or decision_date.isoformat(),
                fetched_ts=fetched_ts,
                source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?CIK={cik}",
            )
        return out

    def _count_recent(
        self, *, cik: str, forms: set[str], cutoff: dt.date,
        on_or_before: dt.date,
    ) -> tuple[int, str | None]:
        """Return ``(count, latest_filing_date_iso)`` for filings whose
        form is in ``forms`` and whose filing_date is within
        ``(cutoff, on_or_before]``. ``latest`` is None when count==0."""
        url = f"{self.base_url}/CIK{cik}.json"
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent,
                          "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raise IntelUnavailable(
                f"edgar cik={cik} http {e.code}: {e.reason}"
            ) from e
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise IntelUnavailable(
                f"edgar cik={cik} non-json response"
            ) from e
        recent = (payload.get("filings", {}) or {}).get("recent", {}) or {}
        form_list = recent.get("form") or []
        date_list = recent.get("filingDate") or []
        if len(form_list) != len(date_list):
            raise IntelUnavailable(
                f"edgar cik={cik} malformed recent block "
                f"(forms={len(form_list)} dates={len(date_list)})"
            )
        count = 0
        latest: dt.date | None = None
        for form, date_str in zip(form_list, date_list):
            if form.upper() not in forms:
                continue
            try:
                d = dt.date.fromisoformat(date_str)
            except ValueError:
                continue
            if d <= cutoff or d > on_or_before:
                continue
            count += 1
            if latest is None or d > latest:
                latest = d
        return count, (latest.isoformat() if latest else None)


__all__ = ["DEFAULT_ENTITIES", "DEFAULT_FORMS", "EdgarFeed"]
