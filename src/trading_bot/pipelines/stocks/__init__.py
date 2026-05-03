"""Stocks pipeline (Phase 2 strangler-fig).

Phase 2 introduces this package as the canonical home for stocks-pipeline
modules under the Option-2 layout. To avoid a high-blast-radius rename
across hold_debate, scout_debate, intel_ingestor, orchestrator, and
the email/digest layers, the implementation files still live at their
legacy ``trading_bot.personas`` / ``trading_bot.intel`` paths during the
transition. Modules under this package are thin **re-export shims**
that point to those legacy implementations.

  pipelines/stocks/personas/<name>.py   →  trading_bot.personas.<name>
  pipelines/stocks/intel/<name>.py      →  trading_bot.intel.<name>

This lets new callers (the /desk roster, future tests, future runners
that we're writing today in pipelines/stocks/...) target the canonical
namespace without forcing every legacy import site to migrate in lockstep.
As call sites migrate, the legacy modules can become re-export shims
in the opposite direction and eventually be deleted.

Independent of crypto/ and options/ — this package never imports from
those siblings.
"""
