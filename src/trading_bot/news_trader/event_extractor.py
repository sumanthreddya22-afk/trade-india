"""W4.1 — Deterministic news-event extractor.

Aggregates a cluster of ``NewsItem``s into a single ``StructuredEvent`` that
the news_controller LLM consumes. Pure regex + counting; no LLM call here so
the LLM only sees pre-verified, structurally-vetted input.

The structured event is what the PDF (page 9-10, 16) calls the news-driven
controller's input. Verification of the underlying claim is the LLM's job;
this module's job is to make the LLM's task tractable by giving it a
clean event schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from trading_bot.intelligence import NewsItem


# ---- Event-type lexicon. Order matters: more specific patterns first.
_EVENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("merger_acquisition",
     re.compile(r"\b(acquire|acquisition|acquires|merger|merge\s+with|takeover|"
                r"buyout|bidding\s+for)\b", re.I)),
    ("strategic_alternatives",
     re.compile(r"\b(exploring\s+(?:strategic|sale|options|alternatives)|"
                r"strategic\s+alternatives|"
                r"considering\s+sale|review\s+of\s+strategic)\b", re.I)),
    ("earnings",
     re.compile(r"\b(earnings|q[1-4]\s+(?:results|beat|miss)|revenue\s+up|"
                r"EPS|beats?\s+estimates?|misses?\s+estimates?)\b", re.I)),
    ("guidance",
     re.compile(r"\b(guidance|raises?\s+(?:full[- ]year|fy|outlook)|"
                r"cuts?\s+(?:q\d|guidance|outlook)|lowers?\s+(?:outlook|"
                r"guidance))\b", re.I)),
    ("litigation",
     re.compile(r"\b(lawsuit|class\s+action|sec\s+investigation|"
                r"securities\s+fraud|sued)\b", re.I)),
    ("leadership_change",
     re.compile(r"\b(names?\s+new\s+(?:ceo|cfo|coo|chairman)|new\s+ceo|"
                r"appoints?|resigns?|steps\s+down)\b", re.I)),
    ("dividend",
     re.compile(r"\b(declares?\s+dividend|dividend\s+(?:hike|cut)|"
                r"share\s+buyback|repurchase\s+program)\b", re.I)),
    ("product_launch",
     re.compile(r"\b(unveils?|launches?|debut|introduces?)\b", re.I)),
]

_POSITIVE_WORDS = re.compile(
    r"\b(beat|beats|surges?|jumps?|raises?|strong|growth|record|"
    r"upgrade|upgrades|positive|profit|gains?)\b",
    re.I,
)
_NEGATIVE_WORDS = re.compile(
    r"\b(miss(es)?|cuts?|plunges?|drops?|tumbles?|downgrade|downgrades|"
    r"negative|loss|warns?|lawsuit|investigation|fraud|breach)\b",
    re.I,
)

# Sources that count as a "primary filing" (high authority signal).
_PRIMARY_FILING_SOURCES = {"sec_edgar", "sec", "edgar"}
_PRIMARY_FILING_PATTERNS = re.compile(r"\b(8-?K|10-?K|10-?Q|13[DG]|filed)\b", re.I)


@dataclass(frozen=True)
class StructuredEvent:
    """Per the PDF page 9-10, 16. Feeds the news_controller LLM prompt.

    All fields are derivable from the source NewsItem cluster — no LLM here.
    """

    entity: str
    event_type: str
    direction: str  # POSITIVE | NEGATIVE | MIXED
    novelty: float  # 0.0 (already widely known) to 1.0 (genuinely new)
    source_count: int
    primary_filing_present: bool
    headline: str  # representative headline (longest of the cluster)
    sources: tuple[str, ...]  # distinct source names, deduped


def detect_event_type(headline: str) -> str:
    """Classify a headline by lexicon. Returns ``general`` if nothing matches."""
    for label, pattern in _EVENT_PATTERNS:
        if pattern.search(headline):
            return label
    return "general"


def detect_direction(text: str) -> str:
    """Tiny sentiment classifier from the headline. Mixed when both polarities
    appear or when neither does."""
    pos = bool(_POSITIVE_WORDS.search(text))
    neg = bool(_NEGATIVE_WORDS.search(text))
    if pos and not neg:
        return "POSITIVE"
    if neg and not pos:
        return "NEGATIVE"
    return "MIXED"


def _is_primary_filing(item: NewsItem) -> bool:
    if item.source.lower() in _PRIMARY_FILING_SOURCES:
        return True
    return bool(_PRIMARY_FILING_PATTERNS.search(item.headline))


def extract_event(items: list[NewsItem], *, entity: str) -> StructuredEvent | None:
    """Aggregate a cluster of NewsItems about one entity into a single event.

    Returns ``None`` if ``items`` is empty.

    De-dupes on (source, headline) so multiple wires of the same Reuters
    story don't inflate the source count. Novelty is a heuristic based on
    item-count + age spread — the LLM is expected to do the final novelty
    judgement against its training and the verification chain.
    """
    if not items:
        return None

    seen: set[tuple[str, str]] = set()
    deduped: list[NewsItem] = []
    for n in items:
        key = (n.source.lower(), n.headline.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)

    sources = tuple(sorted({n.source for n in deduped}))
    primary = any(_is_primary_filing(n) for n in deduped)

    # Pick the longest headline as the representative — it usually carries the most signal.
    representative = max(deduped, key=lambda n: len(n.headline))
    event_type = detect_event_type(representative.headline)
    direction = detect_direction(
        representative.headline + " " + (representative.summary or "")
    )

    # Heuristic novelty: 1.0 when only one source has it, decaying as the
    # story spreads. This is intentionally simple — the LLM does the real
    # novelty assessment against prior disclosures and consensus.
    if len(deduped) == 1:
        novelty = 1.0
    elif len(deduped) <= 3:
        novelty = 0.7
    elif len(deduped) <= 8:
        novelty = 0.4
    else:
        novelty = 0.1

    return StructuredEvent(
        entity=entity,
        event_type=event_type,
        direction=direction,
        novelty=novelty,
        source_count=len(sources),
        primary_filing_present=primary,
        headline=representative.headline,
        sources=sources,
    )
