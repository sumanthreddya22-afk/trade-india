# ADR 0002: Bot LLM calls go through Claude CLI subprocess (Max subscription quota)

- **Date:** 2026-05-02
- **Status:** accepted
- **Pipelines affected:** shared
- **Author:** operator

## Context

Bot debates need an LLM. Two transport choices:

- Anthropic Python SDK with `ANTHROPIC_API_KEY` (pay-per-token,
  ~$1,300/mo at full three-pipeline operation)
- `claude` CLI invoked as subprocess (consumes Max subscription quota,
  ~$0/mo incremental beyond the existing Max plan)

The Anthropic Agent SDK docs explicitly state subscription auth is "not
allowed" for third-party developer products. Personal-hobby use is a
gray area. The user accepts the policy risk to avoid paying API costs.

Max 5x rate limits (~225 messages / 5 hr window) are tight relative to
naive bot operation (~1,800 calls/day). Survived only through batching.

## Decision

`shared/llm_transport.py` invokes Claude via `subprocess.run(["claude",
"-p", prompt, "--model", model, "--output-format", "json", ...])`. All
bot debate, scout, hold, lesson, threshold-tuner, sentiment-classifier,
adversarial-classifier, summary-writer, and cross-pollination LLM calls
flow through this single function.

Model routing is deterministic in code:

- Judges + outcome analyzer + threshold tuner + quarterly audit lead → Opus 4.7
- Reviewers + classifiers + summaries + drift detector + PR bot → Sonnet 4.6
- Haiku is **not used** anywhere

Five batching strategies enabled to fit Max 5x quota:

1. Batched per-headline sentiment (one call per source per tick)
2. Batched adversarial flag classification
3. Two-call debates (combined-reviewer + separate judge)
4. Already-batched scout debates
5. Verdict cache keyed by `(symbol, intel_signature_hash, asset_class)`

When Max quota window exhausted: `SubscriptionRateLimited` raised;
`llm_skip_window_until` set; subsequent debate calls return
`SkipVerdict("rate_limited", confidence=None)`; bot falls through to
deterministic gates only (risk_manager, hard rules); operator alerted;
auto-resume on window clear.

## Consequences

- ~$0/mo incremental LLM cost (covered by existing Max plan).
- ~3-5× per-call latency vs. direct API (subprocess startup overhead).
  Acceptable for current cadences; matters during fast express-lane
  events.
- Account-flagging risk if Anthropic detects sustained automated use.
  Mitigation: keep usage human-like in pattern; if flagged, swap
  `llm_transport.py` to API-key path (single-file change).
- Bot continues trading during rate-limit windows but without LLM-
  driven decisions — falls through to deterministic gates only.

## Alternatives considered

- Pure API key (Option A): ~$1,300/mo, policy-clean, no rate-limit
  handling needed. Rejected on cost.
- Hybrid (subscription primary + API fallback on rate-limit): adds
  per-month variable cost and complexity. Rejected because graceful
  skip-to-deterministic-gates is simpler and fits the bot's risk model.
- Bedrock / Vertex / Foundry routing: similar costs, no advantage at
  this scale.
