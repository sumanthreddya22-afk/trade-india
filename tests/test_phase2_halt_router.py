"""Phase 2 — halt router."""
from __future__ import annotations

from trading_bot.risk.halt_router import decide


def test_no_kills_accepts() -> None:
    d = decide(active_kill_set=set(), intent_side="buy")
    assert d.verdict == "accept"


def test_any_kill_halts_entries() -> None:
    d = decide(active_kill_set={"recon_mismatch"}, intent_side="buy")
    assert d.verdict == "halt"
    assert "recon_mismatch" in d.reason


def test_any_kill_passes_exits() -> None:
    d = decide(active_kill_set={"recon_mismatch", "clock_skew"},
               intent_side="sell_to_close")
    assert d.verdict == "accept"
    assert "exit_passthrough" in d.reason
