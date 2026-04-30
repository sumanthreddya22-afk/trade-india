"""Post-build verification.

Asserts the generated PDF has reasonable size, page count, and that all the
required factual phrases + figure captions are present in the extracted text.
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

# pdfplumber gripes about embedded webfonts that have no FontBBox descriptor —
# that's purely a PDF-metadata artefact of Chromium-embedded fonts and does
# not affect text extraction. Silence both the python warning and the loguru
# logger that pdfplumber/pdfminer use.
warnings.filterwarnings("ignore", category=UserWarning, module="pdfminer")
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfminer.pdfinterp").setLevel(logging.ERROR)
logging.getLogger("pdfminer.pdffont").setLevel(logging.ERROR)

import pdfplumber  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PDF = REPO_ROOT / "docs" / "project_overview_v2.pdf"


REQUIRED_PHRASES = [
    "claude-opus-4-7",
    "DecisionStore",
    "purged walk-forward".lower(),  # case-insensitive
    "PBO",
    "Deflated Sharpe",
    "wheel-scan",
    "crypto-scan",
    "intel-scan",
    "Truth Social",
    "ApeWisdom",
    "TradeOrchestrator",
    "Optuna",
    "RiskManager",
    "Alpaca",
    "FRED",
    "GDELT",
    "SEC EDGAR",
    "Finnhub",
    "CoinGecko",
    "Fear &",  # alt.me Fear & Greed (HTML may render with literal &)
    "MomentumStrategy",
    "WheelLane",
    "news_trader",
]

REQUIRED_FIGURES = [f"Figure D{i}" for i in range(1, 9)]

MIN_PAGES = 14
MAX_PAGES = 80
MIN_KB = 80
MAX_MB = 25


def main() -> int:
    if not PDF.exists():
        print(f"FAIL  pdf does not exist: {PDF}")
        return 1

    size_kb = PDF.stat().st_size / 1024
    if not (MIN_KB <= size_kb <= MAX_MB * 1024):
        print(f"FAIL  size out of bounds: {size_kb:.0f} KB (expected {MIN_KB}–{MAX_MB*1024} KB)")
        return 1
    print(f"OK    size {size_kb:,.0f} KB")

    text_pages: list[str] = []
    with pdfplumber.open(PDF) as pdf:
        n_pages = len(pdf.pages)
        for p in pdf.pages:
            text_pages.append(p.extract_text() or "")
    if not (MIN_PAGES <= n_pages <= MAX_PAGES):
        print(f"FAIL  page count out of bounds: {n_pages} (expected {MIN_PAGES}–{MAX_PAGES})")
        return 1
    print(f"OK    pages {n_pages}")

    text = "\n".join(text_pages)
    text_lower = text.lower()

    missing_phrases = []
    for phrase in REQUIRED_PHRASES:
        needle = phrase.lower()
        if needle not in text_lower:
            missing_phrases.append(phrase)
    if missing_phrases:
        print(f"FAIL  missing required phrases: {missing_phrases}")
        return 1
    print(f"OK    all {len(REQUIRED_PHRASES)} required phrases present")

    missing_figs = [f for f in REQUIRED_FIGURES if f not in text]
    if missing_figs:
        print(f"FAIL  missing figure captions: {missing_figs}")
        return 1
    print(f"OK    all {len(REQUIRED_FIGURES)} figure captions present")

    print(f"\nALL OK — {PDF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
