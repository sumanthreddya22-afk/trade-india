#!/usr/bin/env python3
"""Phase 0 crypto unwind — bring crypto exposure to <= 15% of equity.

Plan v4 §6 caps crypto gross at 15% of equity. The repo audit showed the
account at ~58%. This one-shot tool computes the proportional sell-list
needed to hit the cap, writes the proposed orders to
``runs/phase0_crypto_unwind_<utc_ts>.json``, and (when ``--submit`` is
passed and the operator types ``yes``) submits them via Alpaca.

Default behaviour is **dry-run**. Submission requires both ``--submit``
and an interactive ``yes`` confirmation.

Why proportional vs. priority-based:
  Selling the entire smallest position keeps tax-lot accounting simpler
  but concentrates remaining exposure in the largest names. Proportional
  reduction preserves the relative weights and is the right default for
  an exposure-cap correction (vs. a strategy-driven exit).

Why a separate script and not a kernel module:
  This is a one-time Phase 0 cleanup; the Phase 1+ kernel will enforce
  the cap automatically. Keeping it out of the kernel keeps the kernel
  pure (no Alpaca-specific custom-logic for cleanup).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = REPO_ROOT / "runs"

CRYPTO_CAP_PCT = 0.15  # Plan v4 §6
PAPER_BASE = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class SellProposal:
    symbol: str
    market_value: float
    current_price: float
    proposed_sell_qty: float
    proposed_sell_mv: float
    rationale: str


def _alpaca_headers() -> dict[str, str]:
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        sys.exit(
            "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in the environment "
            "(paper account credentials)."
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _get(path: str, headers: dict[str, str]) -> Any:
    url = PAPER_BASE.rstrip("/") + path
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def _build_proposals(
    equity: float, positions: list[dict[str, Any]], cap_pct: float
) -> tuple[list[SellProposal], float, float]:
    cap_value = equity * cap_pct
    crypto = [p for p in positions if p.get("asset_class") == "crypto"]
    crypto_gross = sum(abs(float(p.get("market_value") or 0)) for p in crypto)
    excess = crypto_gross - cap_value
    proposals: list[SellProposal] = []
    if excess <= 0:
        return proposals, crypto_gross, cap_value
    for p in crypto:
        mv = abs(float(p.get("market_value") or 0))
        cur = float(p.get("current_price") or 0)
        if mv <= 0 or cur <= 0:
            continue
        share = mv / crypto_gross
        sell_mv = excess * share
        sell_qty = sell_mv / cur
        # Crypto fractional precision varies by pair; round to 6 decimal places.
        sell_qty = round(sell_qty, 6)
        if sell_qty <= 0:
            continue
        # Don't try to sell short on a near-zero position.
        held_qty = float(p.get("qty") or 0)
        if held_qty <= 0:
            continue
        if sell_qty > held_qty:
            sell_qty = round(held_qty, 6)
            sell_mv = sell_qty * cur
        proposals.append(SellProposal(
            symbol=p["symbol"],
            market_value=mv,
            current_price=cur,
            proposed_sell_qty=sell_qty,
            proposed_sell_mv=round(sell_mv, 2),
            rationale=(
                f"crypto gross {crypto_gross:.2f} is "
                f"{(crypto_gross/equity)*100:.1f}% of equity "
                f"(cap {cap_pct*100:.0f}%); proportional sell at "
                f"{share*100:.2f}% share of excess"
            ),
        ))
    return proposals, crypto_gross, cap_value


def _render_text(equity: float, gross: float, cap_value: float,
                 proposals: list[SellProposal]) -> str:
    lines = [
        "# Phase 0 crypto unwind — DRY RUN",
        f"  equity         : ${equity:,.2f}",
        f"  crypto gross   : ${gross:,.2f}  ({gross/equity*100:.2f}% of equity)",
        f"  cap (15%)      : ${cap_value:,.2f}",
        f"  excess to sell : ${gross - cap_value:,.2f}",
        "",
        f"  proposals ({len(proposals)}):",
    ]
    for p in proposals:
        lines.append(
            f"    SELL {p.symbol:<10s} qty={p.proposed_sell_qty:>14.6f} "
            f"@~{p.current_price:>10.4f}  est_mv=${p.proposed_sell_mv:>9.2f}  "
            f"(holding ${p.market_value:.2f})"
        )
    total = sum(p.proposed_sell_mv for p in proposals)
    lines.append("")
    lines.append(f"  estimated total proceeds: ${total:,.2f}")
    return "\n".join(lines) + "\n"


def _save_proposals(proposals: list[SellProposal], path: Path,
                    equity: float, gross: float, cap_value: float) -> None:
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "equity": equity,
        "crypto_gross": gross,
        "cap_value": cap_value,
        "cap_pct": CRYPTO_CAP_PCT,
        "proposals": [
            {
                "symbol": p.symbol,
                "market_value": p.market_value,
                "current_price": p.current_price,
                "proposed_sell_qty": p.proposed_sell_qty,
                "proposed_sell_mv": p.proposed_sell_mv,
                "rationale": p.rationale,
            }
            for p in proposals
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _submit_orders(proposals: list[SellProposal], headers: dict[str, str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for p in proposals:
        client_order_id = (
            f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')}"
            f"_phase0unwind_{p.symbol}_{int(time.time())}"
        )
        body = {
            "symbol": p.symbol,
            "qty": str(p.proposed_sell_qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        }
        try:
            r = requests.post(
                PAPER_BASE.rstrip("/") + "/v2/orders",
                headers=headers, json=body, timeout=20,
            )
            ok = r.ok
            payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        except Exception as e:
            ok = False
            payload = {"error": str(e)}
        results.append({
            "symbol": p.symbol,
            "client_order_id": client_order_id,
            "ok": ok,
            "response": payload,
        })
        # be a polite client
        time.sleep(0.3)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--submit", action="store_true",
                    help="actually submit orders after interactive confirmation")
    ap.add_argument("--cap-pct", type=float, default=CRYPTO_CAP_PCT,
                    help="override crypto cap fraction (default 0.15)")
    args = ap.parse_args()

    headers = _alpaca_headers()
    acct = _get("/v2/account", headers)
    pos = _get("/v2/positions", headers)
    equity = float(acct.get("equity") or 0)

    proposals, gross, cap_value = _build_proposals(equity, pos, args.cap_pct)

    text = _render_text(equity, gross, cap_value, proposals)
    print(text)

    if not proposals:
        print("No action needed; crypto already under the cap.")
        return 0

    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS_DIR / f"phase0_crypto_unwind_{ts}.json"
    _save_proposals(proposals, out, equity, gross, cap_value)
    print(f"proposals written to {out}")

    if not args.submit:
        print("Dry-run only. Re-run with --submit to actually sell.")
        return 0

    print()
    print("=" * 60)
    print("READY TO SUBMIT THE ABOVE ORDERS TO ALPACA PAPER.")
    print("Type 'yes' (without quotes) to confirm; any other input aborts.")
    print("=" * 60)
    try:
        confirm = input("confirm > ").strip().lower()
    except EOFError:
        confirm = ""
    if confirm != "yes":
        print("Aborted; no orders submitted.")
        return 1
    results = _submit_orders(proposals, headers)
    out_results = RUNS_DIR / f"phase0_crypto_unwind_results_{ts}.json"
    out_results.write_text(json.dumps(results, indent=2, sort_keys=True))
    ok = sum(1 for r in results if r.get("ok"))
    print(f"submitted {ok}/{len(results)} orders; results: {out_results}")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
