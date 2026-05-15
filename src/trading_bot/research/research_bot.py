"""Research-bot pipeline scaffold (v4 Phase D).

Public surface used by the daemon:

  * ``run_source_scouts(ledger_db, policy_dir)`` — every 6h
  * ``run_intake_pipeline(ledger_db, policy_dir)`` — nightly

Internals (scout → candidate → blueprint → codegen → paper-validation →
auto-register) live in submodules. This module wires them together with
fail-closed semantics: the daemon must survive a misconfigured scout.

The scaffold reads ``policy/research_bot_sources_v1.json``. Each source
implements a thin adapter (RSS / API) — Phase D adds real fetchers
beyond what ships here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from trading_bot.ledger import connect_writer
from trading_bot.ledger.research_bot import (
    candidate_exists, write_blueprint, write_candidate, write_codegen,
    write_source_scout,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_sources(policy_dir: Path) -> Mapping[str, Any]:
    p = policy_dir / "research_bot_sources_v1.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        log.warning("research_bot_sources_v1.json invalid: %s", e)
        return {}


def _content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


# ----- Source scouts (real fetchers) --------------------------------------

_HTTP_TIMEOUT_S = 8.0
_USER_AGENT = "trading-bot-research/0.1 (autonomous strategy discovery)"


def _enumerate_arxiv_items(cfg: Mapping[str, Any]) -> list[dict]:
    """Pull recent q-fin papers via arXiv API + Atom feed."""
    try:
        import feedparser
    except ImportError:
        return []
    categories = list(cfg.get("categories", ["q-fin.PM"]))
    min_words = int(cfg.get("min_abstract_words", 100))
    out: list[dict] = []
    for cat in categories:
        url = (
            f"http://export.arxiv.org/api/query?search_query=cat:{cat}"
            f"&max_results=20&sortBy=submittedDate&sortOrder=descending"
        )
        try:
            parsed = feedparser.parse(url, request_headers={
                "User-Agent": _USER_AGENT,
            })
        except Exception as e:  # noqa: BLE001
            log.warning("arxiv parse %s failed: %s", cat, e)
            continue
        for entry in parsed.entries or []:
            abstract = entry.get("summary", "")
            if len(abstract.split()) < min_words:
                continue
            out.append({
                "title": entry.get("title", "").strip(),
                "body": abstract.strip(),
                "url": entry.get("link", ""),
                "tags": [cat],
                "quality_score": 0.7,  # arXiv default: peer-review-adjacent
            })
    return out


def _enumerate_substack_items(feeds: Iterable[str]) -> list[dict]:
    try:
        import feedparser
    except ImportError:
        return []
    out: list[dict] = []
    for feed_url in feeds:
        url = feed_url if feed_url.startswith("http") else f"https://{feed_url}"
        try:
            parsed = feedparser.parse(url, request_headers={
                "User-Agent": _USER_AGENT,
            })
        except Exception as e:  # noqa: BLE001
            log.info("substack parse %s failed: %s", url, e)
            continue
        for entry in (parsed.entries or [])[:10]:
            body = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
            if not entry.get("title") or not body:
                continue
            out.append({
                "title": entry.get("title", "").strip(),
                "body": body.strip()[:8000],
                "url": entry.get("link", ""),
                "tags": ["substack"],
                "quality_score": 0.5,
            })
    return out


def _enumerate_reddit_items(subs: Iterable[str], cfg: Mapping[str, Any]) -> list[dict]:
    import urllib.error
    import urllib.request
    min_up = int(cfg.get("min_upvotes", 100))
    min_comments = int(cfg.get("min_comments", 20))
    out: list[dict] = []
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=15"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _USER_AGENT,
            })
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as r:
                body = r.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            log.info("reddit %s failed: %s", sub, e)
            continue
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError:
            continue
        for child in (envelope.get("data") or {}).get("children", []):
            data = (child or {}).get("data") or {}
            upvotes = int(data.get("ups", 0) or 0)
            comments = int(data.get("num_comments", 0) or 0)
            if upvotes < min_up or comments < min_comments:
                continue
            text = data.get("selftext") or ""
            if not text and data.get("url", "").startswith("http"):
                text = data.get("title", "")
            out.append({
                "title": data.get("title", ""),
                "body": text[:8000],
                "url": f"https://reddit.com{data.get('permalink', '')}",
                "tags": [f"reddit:{sub}"],
                "quality_score": min(1.0, (upvotes / 1000.0) * 0.5 + 0.3),
            })
    return out


def _enumerate_github_items(orgs: Iterable[str], cfg: Mapping[str, Any]) -> list[dict]:
    import os
    import urllib.error
    import urllib.request
    token = os.environ.get("TRADING_BOT_GITHUB_TOKEN")
    headers = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"token {token}"
    min_stars = int(cfg.get("min_stars", 100))
    out: list[dict] = []
    for org in orgs:
        url = (
            f"https://api.github.com/search/repositories"
            f"?q=user:{org}+stars:>{min_stars}&sort=updated&per_page=10"
        )
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as r:
                body = r.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            log.info("github org=%s failed: %s", org, e)
            continue
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError:
            continue
        for repo in (envelope.get("items") or [])[:5]:
            desc = repo.get("description") or ""
            if not desc:
                continue
            out.append({
                "title": f"{repo.get('full_name', '')}: {desc[:120]}",
                "body": desc,
                "url": repo.get("html_url", ""),
                "tags": ["github", *repo.get("topics", [])],
                "quality_score": min(1.0, (int(repo.get("stargazers_count", 0)) / 5000.0) * 0.5 + 0.3),
            })
    return out


# Per-source scout dispatcher.
_SCOUT_DISPATCH = {
    "arxiv": lambda cfg: _enumerate_arxiv_items(cfg),
    "substack": lambda cfg: _enumerate_substack_items(cfg.get("feeds", [])),
    "reddit": lambda cfg: _enumerate_reddit_items(
        cfg.get("subreddits", []), cfg,
    ),
    "github": lambda cfg: _enumerate_github_items(cfg.get("orgs", []), cfg),
}


def run_source_scouts(
    ledger_db: Path,
    *,
    policy_dir: Path,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Run one scout tick across configured sources. Each scout returns
    a list of raw items; the dispatcher dedups, writes
    ``strategy_candidate`` rows, and emits one ``source_scout_event``
    per source."""
    sources_cfg = _load_sources(policy_dir).get("sources", {})
    out: dict = {"sources_run": [], "n_candidates": 0}

    conn = connect_writer(ledger_db)
    try:
        for category, source_map in sources_cfg.items():
            if not isinstance(source_map, dict):
                continue
            for source_name, source_cfg in source_map.items():
                scout = _SCOUT_DISPATCH.get(source_name)
                if scout is None:
                    continue
                try:
                    items = list(scout(source_cfg) or [])
                except Exception as e:  # noqa: BLE001
                    log.warning("scout %s failed: %s", source_name, e)
                    continue
                items_seen = len(items)
                created = 0
                deduped = 0
                for item in items:
                    raw_hash = _content_hash(
                        source_name,
                        item.get("title", ""),
                        item.get("body", ""),
                    )
                    if candidate_exists(conn, raw_hash):
                        deduped += 1
                        continue
                    try:
                        write_candidate(
                            conn,
                            source=f"{source_name}",
                            source_ref=item.get("url", ""),
                            raw_content_hash=raw_hash,
                            title=item.get("title", "")[:200],
                            summary_md=item.get("body", "")[:8000],
                            taxonomy_tags=item.get("tags", []),
                            quality_score=float(item.get("quality_score", 0.5)),
                            status="pending",
                        )
                        created += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("write_candidate %s failed: %s",
                                    source_name, e)
                write_source_scout(
                    conn, source=source_name,
                    items_seen=items_seen,
                    items_above_quality=items_seen,  # quality filter TODO
                    items_deduplicated=deduped,
                    items_candidates_created=created,
                )
                out["sources_run"].append(source_name)
                out["n_candidates"] += created
        conn.commit()
    finally:
        conn.close()
    return out


def _family_id_from_title(title: str) -> str:
    """Derive a stable, snake_case family_id from a candidate title."""
    import re
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", title.lower()).strip("_")
    return cleaned[:40] or "untitled_auto"


def run_intake_pipeline(
    ledger_db: Path,
    *,
    policy_dir: Path,
    now: Optional[dt.datetime] = None,
    max_per_tick: int = 1,
) -> dict:
    """Pull pending candidates, write a blueprint, run codegen via the
    strategy_implementer persona, and validate the generated code.

    Each step degrades gracefully when an upstream piece (persona,
    LLM, ruff, pytest) is unavailable. ``max_per_tick`` caps how many
    candidates the bot tries per tick — keeps the LLM budget bounded.
    """
    out: dict = {
        "n_pending": 0, "n_blueprinted": 0,
        "n_codegen_attempted": 0, "n_codegen_accepted": 0,
    }
    try:
        from trading_bot.ledger.research_bot import write_blueprint
        from trading_bot.research.codegen import generate_for_blueprint
    except ImportError as e:
        return {"error": f"import: {e}"}

    conn = connect_writer(ledger_db)
    try:
        try:
            cur = conn.execute(
                "SELECT ledger_seq, title, summary_md, taxonomy_tags_json "
                "FROM strategy_candidate WHERE status='pending' "
                "ORDER BY ledger_seq DESC LIMIT ?",
                (int(max_per_tick),),
            )
            pending = cur.fetchall()
        except sqlite3.OperationalError:
            pending = []
        out["n_pending"] = len(pending)

        for row in pending:
            candidate_id, title, summary_md, tags_json = row
            family_id = _family_id_from_title(title)
            try:
                tags = json.loads(tags_json) if tags_json else []
            except json.JSONDecodeError:
                tags = []

            # Write a minimal blueprint synchronously. The richer
            # adversarial intake debate (quant_research_lead vs
            # risk_validator) lives in ``research.run_intake`` and
            # remains an operator-driven step; this pipeline anchors
            # the candidate to a blueprint so codegen has a stable
            # artifact to reference + so the audit trail is complete.
            try:
                blueprint_id = write_blueprint(
                    conn,
                    candidate_id=int(candidate_id),
                    blueprint_md=f"# {title}\n\n{summary_md}",
                    params={}, universe_filter={"tags": tags},
                    data_needs=["price_bars"],
                    data_available=True,
                    intake_transcript_id="auto-intake",
                    intake_verdict="approved",
                )
                conn.commit()
                out["n_blueprinted"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("blueprint write failed: %s", e)
                continue

            try:
                report = generate_for_blueprint(
                    ledger_db=ledger_db,
                    blueprint_id=blueprint_id,
                    blueprint_md=f"# {title}\n\n{summary_md}",
                    family_id=family_id,
                )
                out["n_codegen_attempted"] += 1
                if report.accepted:
                    out["n_codegen_accepted"] += 1
                    conn.execute(
                        "UPDATE strategy_candidate SET status='implemented' "
                        "WHERE ledger_seq=?",
                        (int(candidate_id),),
                    )
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                log.warning("codegen for candidate %s failed: %s",
                            candidate_id, e)
        return out
    finally:
        conn.close()


__all__ = ["run_intake_pipeline", "run_source_scouts"]
