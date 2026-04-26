"""VIP tweet monitor — alert-only, never trades.

Polls Truth Social RSS feeds for a configured list of handles. Scores each
post by keyword tier (HIGH / MED / LOW). On any new HIGH-severity post,
sends an email alert. State (last-seen post IDs per handle) lives in
`data/vip_seen.json` so re-fires are idempotent.

Future-proofing notes:
- Twitter/X support is intentionally not wired. The Free API tier is too
  rate-limited (~50 reads/month as of 2024). If the user upgrades to
  Basic, add an X provider that returns the same dict shape as Truth
  Social and call it from gather_all().
- The reaction policy here is **alert-only**. Bot does NOT auto-halt,
  auto-veto, or place defensive orders. The email is a heads-up; humans
  decide what to do. Per the trader's risk analysis, mechanical
  reactions need backtest validation (Plan 5b) before they are safe.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
import yaml

VIP_HANDLES_PATH = Path("strategy/vip_handles.yaml")
VIP_SEEN_PATH = Path("data/vip_seen.json")
HTTP_TIMEOUT = 10


# Keyword tiers — case-insensitive substring match.
# Tuned conservatively: we'd rather miss a HIGH than fire a HIGH on
# bullish-but-not-market-moving language.
_HIGH_KEYWORDS: tuple[str, ...] = (
    "tariff", "sanction", "embargo",
    "federal reserve", "fed chair", "powell", "fire ", " fired",
    "imposing", "executive order", "emergency",
    "crash", "collapse", "bailout", "default",
    "war ", "invasion", "strike",
    "$spy", "$qqq", "$btc", "$eth",
)
_MED_KEYWORDS: tuple[str, ...] = (
    "inflation", "deflation", "recession",
    "rate cut", "rate hike", "rate decision",
    "jobs report", "unemployment", "gdp",
    "earnings", "guidance",
    "china", "russia", "iran", "north korea",
    "treasury", "deficit",
)


@dataclass(frozen=True)
class VipHandle:
    name: str
    platform: str  # "truth_social"
    rss_url: str


@dataclass(frozen=True)
class VipPost:
    handle: str
    platform: str
    post_id: str
    url: str
    published: datetime | None
    text: str          # title + description, joined
    severity: str      # "high" | "med" | "low"
    severity_reason: str


@dataclass(frozen=True)
class VipScanResult:
    handles_polled: int
    posts_seen: int
    new_posts: list[VipPost] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def high_count(self) -> int:
        return sum(1 for p in self.new_posts if p.severity == "high")

    @property
    def med_count(self) -> int:
        return sum(1 for p in self.new_posts if p.severity == "med")


def load_handles(path: Path = VIP_HANDLES_PATH) -> list[VipHandle]:
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    out: list[VipHandle] = []
    for entry in raw.get("handles", []):
        if not isinstance(entry, dict):
            continue
        try:
            out.append(VipHandle(
                name=str(entry["name"]),
                platform=str(entry.get("platform", "truth_social")),
                rss_url=str(entry["rss_url"]),
            ))
        except Exception:
            continue
    return out


def load_seen(path: Path = VIP_SEEN_PATH) -> dict[str, str]:
    """Returns {handle_name -> last_seen_post_id}."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_seen(seen: dict[str, str], path: Path = VIP_SEEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, indent=2))


def score(text: str) -> tuple[str, str]:
    """Return (severity, reason). Severity is 'high' | 'med' | 'low'."""
    t = text.lower()
    for kw in _HIGH_KEYWORDS:
        if kw in t:
            return ("high", f"keyword: {kw.strip()!r}")
    for kw in _MED_KEYWORDS:
        if kw in t:
            return ("med", f"keyword: {kw.strip()!r}")
    return ("low", "no high/med keywords")


def _parse_rss(xml_text: str) -> list[dict[str, Any]]:
    """Pull <item> children out of a generic RSS 2.0 document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        post_id = (item.findtext("guid") or item.findtext("link") or "").strip()
        if not post_id:
            continue
        out.append({
            "post_id": post_id,
            "title": (item.findtext("title") or "").strip(),
            "url": (item.findtext("link") or "").strip(),
            "published_str": (item.findtext("pubDate") or "").strip(),
            "description": (item.findtext("description") or "").strip(),
        })
    return out


def _parse_published(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fetch_handle_posts(handle: VipHandle, timeout: int = HTTP_TIMEOUT) -> list[VipPost]:
    """Fetch the RSS feed for a handle and return parsed posts (unscored ranking,
    severity computed). Returns empty list on any fetch/parse failure."""
    headers = {
        "User-Agent": "trading-bot/0.1 (+local research, paper account)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    try:
        r = requests.get(handle.rss_url, headers=headers, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return []

    raw_items = _parse_rss(r.text)
    out: list[VipPost] = []
    for item in raw_items:
        text = f"{item['title']} {item['description']}".strip()
        severity, reason = score(text)
        out.append(VipPost(
            handle=handle.name,
            platform=handle.platform,
            post_id=item["post_id"],
            url=item["url"],
            published=_parse_published(item["published_str"]),
            text=text,
            severity=severity,
            severity_reason=reason,
        ))
    return out


def gather_new_posts(
    handles: list[VipHandle],
    seen: dict[str, str],
) -> tuple[list[VipPost], dict[str, str], list[str]]:
    """Pull all configured handles. Returns (new_posts, updated_seen, errors).

    Idempotent: only posts whose post_id != seen[handle] (and weren't already
    seen earlier in the same RSS) are returned. Updates last-seen to the most
    recent post_id per handle.
    """
    new_posts: list[VipPost] = []
    updated = dict(seen)
    errors: list[str] = []

    for h in handles:
        try:
            posts = fetch_handle_posts(h)
        except Exception as e:
            errors.append(f"{h.name}: {e}")
            continue
        if not posts:
            continue

        last_seen = seen.get(h.name)
        # RSS items are typically in reverse-chronological order. Walk until we
        # hit the previously-seen post; everything before it is new.
        new_for_handle: list[VipPost] = []
        for p in posts:
            if last_seen is not None and p.post_id == last_seen:
                break
            new_for_handle.append(p)
        new_posts.extend(new_for_handle)
        # Update last-seen to the freshest post (top of feed) regardless.
        updated[h.name] = posts[0].post_id

    return new_posts, updated, errors


def scan(
    *,
    handles_path: Path = VIP_HANDLES_PATH,
    seen_path: Path = VIP_SEEN_PATH,
) -> VipScanResult:
    handles = load_handles(handles_path)
    if not handles:
        return VipScanResult(
            handles_polled=0, posts_seen=0,
            errors=[f"No handles configured at {handles_path}"],
        )
    seen = load_seen(seen_path)
    new_posts, updated_seen, errs = gather_new_posts(handles, seen)
    if updated_seen != seen:
        save_seen(updated_seen, seen_path)
    return VipScanResult(
        handles_polled=len(handles),
        posts_seen=sum(1 for p in new_posts) + len(seen),
        new_posts=new_posts,
        errors=errs,
    )
