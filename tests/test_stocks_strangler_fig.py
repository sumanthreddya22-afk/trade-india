"""Phase 2 stocks strangler-fig: verify the new canonical namespace.

The implementation files still live at ``trading_bot.personas`` /
``trading_bot.intel`` during the transition; the new
``trading_bot.pipelines.stocks.{personas, intel}`` namespace contains
re-export shims that forward to those legacy locations.

These tests guard the strangler-fig contract:
  - Every legacy persona module is reachable via the new canonical name.
  - The same Python object identity is preserved (PERSONA dict).
  - PROMPT / VERSION constants forward correctly so legacy debate code
    that reads them via ``module.PROMPT`` keeps working.
  - Discovery via ``shared.personas._base.discover`` finds all 8 stocks
    personas under the new namespace (not just the legacy one).
"""
from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------


_STOCKS_PERSONA_MODULES = (
    "hold_aggressive",
    "hold_conservative",
    "hold_judge",
    "hold_neutral",
    "lesson_analyst",
    "scout_analyst",
    "scout_judge",
    "scout_skeptic",
)


@pytest.mark.parametrize("name", _STOCKS_PERSONA_MODULES)
def test_persona_shim_exposes_persona_dict(name: str):
    """The new canonical name imports cleanly + carries PERSONA."""
    new_mod = importlib.import_module(
        f"trading_bot.pipelines.stocks.personas.{name}",
    )
    assert hasattr(new_mod, "PERSONA"), f"{name} shim is missing PERSONA"
    assert isinstance(new_mod.PERSONA, dict)


@pytest.mark.parametrize("name", _STOCKS_PERSONA_MODULES)
def test_persona_shim_preserves_identity_with_legacy(name: str):
    """The PERSONA dict object on the shim is the same object as legacy."""
    new_mod = importlib.import_module(
        f"trading_bot.pipelines.stocks.personas.{name}",
    )
    legacy_mod = importlib.import_module(f"trading_bot.personas.{name}")
    assert new_mod.PERSONA is legacy_mod.PERSONA


@pytest.mark.parametrize("name", _STOCKS_PERSONA_MODULES)
def test_persona_shim_forwards_prompt_and_version(name: str):
    """Legacy debate code reads module.PROMPT + module.VERSION directly;
    the shim must forward those symbols."""
    new_mod = importlib.import_module(
        f"trading_bot.pipelines.stocks.personas.{name}",
    )
    legacy_mod = importlib.import_module(f"trading_bot.personas.{name}")
    assert new_mod.PROMPT is legacy_mod.PROMPT
    assert new_mod.VERSION == legacy_mod.VERSION


def test_discover_finds_all_stocks_personas_under_new_namespace():
    """The /desk roster uses ``discover()`` over a package — verify it
    returns all 8 stocks personas when pointed at the canonical home."""
    from trading_bot.pipelines.stocks import personas as stocks_personas
    from trading_bot.shared.personas._base import discover

    found = discover(stocks_personas)
    debate_roles = {p.debate_role for p in found}
    assert debate_roles == {
        "hold_aggressive", "hold_conservative", "hold_judge", "hold_neutral",
        "scout_skeptic", "scout_analyst", "scout_judge",
        "lesson_analyst",
    }
    # All eight personas declare pipeline="stocks"
    for p in found:
        assert p.pipeline == "stocks"


# ---------------------------------------------------------------------------
# Intel
# ---------------------------------------------------------------------------


_STOCKS_INTEL_MODULES = (
    "aggregator",
    "adversarial",
    "pool",
    "scout_debate",
    "sec_cik_map",
    "sources",
)


@pytest.mark.parametrize("name", _STOCKS_INTEL_MODULES)
def test_intel_shim_module_imports_cleanly(name: str):
    new_mod = importlib.import_module(
        f"trading_bot.pipelines.stocks.intel.{name}",
    )
    assert new_mod is not None


def test_intel_aggregator_re_exports_source_weights():
    from trading_bot.intel import aggregator as legacy
    from trading_bot.pipelines.stocks.intel import aggregator as canonical
    assert canonical.SOURCE_WEIGHTS is legacy.SOURCE_WEIGHTS
    assert canonical.DEFAULT_SOURCE_WEIGHT == legacy.DEFAULT_SOURCE_WEIGHT


def test_intel_pool_re_exports_lookup():
    from trading_bot.intel import pool as legacy
    from trading_bot.pipelines.stocks.intel import pool as canonical
    assert canonical.lookup is legacy.lookup
    assert canonical.lookup_score is legacy.lookup_score


def test_intel_namespace_imports_all_modules():
    """Importing the package surface should expose every submodule."""
    from trading_bot.pipelines.stocks import intel
    for name in _STOCKS_INTEL_MODULES:
        assert hasattr(intel, name), f"missing {name} on stocks.intel"


# ---------------------------------------------------------------------------
# Backward-compat: legacy import sites still work
# ---------------------------------------------------------------------------


def test_legacy_persona_import_still_works():
    """The strangler-fig contract: existing call sites must NOT break."""
    from trading_bot.personas import hold_aggressive, scout_skeptic
    assert hold_aggressive.PROMPT
    assert scout_skeptic.PERSONA["pipeline"] == "stocks"


def test_legacy_intel_import_still_works():
    from trading_bot.intel import aggregator, pool
    assert aggregator.SOURCE_WEIGHTS
    assert callable(pool.lookup_score)
