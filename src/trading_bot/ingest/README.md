# `ingest/` — L1 + L1.5 Data Ingest

**Status:** Empty skeleton — L1 (Alpaca bars/quotes/account/corporate actions)
lands in **Phase 1 / Phase 3**; L1.5 (alt-data) lands in **Phase 5–7**.

## Mandate (Plan v4 §1B, §3)

Every ingested row carries:

- `source_id` (matches an entry in `policy/source_reliability.lock`)
- `source_tier` at ingestion time (primary | secondary | tertiary)
- `ingestion_ts` (when we received it)
- `claimed_event_ts` (when the source says the event happened)
- `verification_status` (pending | cross_verified | contradicted | unverified)
- `raw_payload_hash` (sha256 for tamper detection)

## Modules (lands across Phase 1 / 1.5 / 3)

L1 (primary tier; consumed by the kernel):

- `alpaca_bars.py`, `alpaca_quotes.py`, `alpaca_account.py`,
  `corporate_actions.py`

L1.5 (alt-data; only `kernel_admissible_tiers` reach the kernel; tertiary
sources are L3-only):

- `sec_edgar.py`, `fred.py`, `bls.py`, `bea.py`, `eia.py`, `cftc_cot.py`,
  `finra_short_interest.py`, `etf_holdings.py`, `news_rss.py`, `gdelt.py`,
  `reddit_json.py`, `stocktwits.py`, `hacker_news.py`, `substack_rss.py`,
  `arxiv_qfin.py`

## Guardrails

1. Tertiary-tier signals (Reddit, StockTwits, HN, Substack) are admissible
   only to L3 hypothesis intake; the L5 kernel rejects features derived from
   tertiary sources at import time.
2. A source whose 30-day false-claim rate exceeds the lock threshold is
   auto-demoted one tier; demotion below `kernel_admissible_tiers` halts
   strategies whose features depend on it.
3. Re-tiering is a loosen action and waits the validation-policy cooldown;
   demotion is a tighten action and is immediate.
