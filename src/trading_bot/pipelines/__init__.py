"""Per-asset-class trading pipelines. Each pipeline is self-contained
and owns its debates, ingest, lesson loop, breaker, streamer, personas,
regime detection, config, and DB tables.

- stocks/  (extracted via strangler-fig in Phase 2)
- crypto/  (cleanroom build, Phase 1)
- options/ (cleanroom build, Phase 3 — wheel state machine native)
"""
