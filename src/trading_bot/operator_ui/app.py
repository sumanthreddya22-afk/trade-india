"""FastAPI app — the v4 operator dashboard.

Localhost-only by default. Every state-changing route is a POST that
writes to the same hash-chained tables the CLI uses.

Routes:
  GET  /                    status page (auto-refresh every 10s)
  GET  /risk                risk profile selector
  POST /risk/apply          apply a profile (writes lock + recomputes hashes)
  GET  /halt                halt / resume page
  POST /halt/halt           fire manual_operator_halt
  POST /halt/resume         clear manual_operator_halt
  GET  /strategy            strategy list + submit form
  POST /strategy/submit     register a new strategy hypothesis
  GET  /api/status          status snapshot as JSON
  GET  /healthz             liveness probe (returns 200 even if ledger missing)
"""
from __future__ import annotations

import os
import logging

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from trading_bot.operator import controls
from trading_bot.operator_ui import templates as tmpl

log = logging.getLogger(__name__)

app = FastAPI(
    title="trading-bot v4 — operator UI",
    docs_url=None, redoc_url=None,  # localhost only; no need for /docs
)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def api_status():
    return JSONResponse(controls.status_snapshot())


@app.get("/", response_class=HTMLResponse)
def home():
    snap = controls.status_snapshot()
    return tmpl.status_page(snap)


@app.get("/digest", response_class=HTMLResponse)
def digest_route(hours: int = 24):
    from trading_bot.operator.digest import build_digest
    d = build_digest(hours=hours)
    return tmpl.digest_page(d)


@app.get("/api/digest")
def api_digest(hours: int = 24):
    from trading_bot.operator.digest import build_digest
    return JSONResponse(build_digest(hours=hours))


@app.get("/risk", response_class=HTMLResponse)
def risk():
    return tmpl.risk_page(controls.risk_profile_show())


@app.post("/risk/apply")
def risk_apply(profile: str = Form(...), note: str = Form(...)):
    operator = os.environ.get("USER", "operator")
    try:
        result = controls.risk_profile_set(
            profile, operator=f"{operator}: {note}",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("risk_profile_set failed")
        return HTMLResponse(
            tmpl.risk_page(controls.risk_profile_show(),
                           flash=f"Error: {type(e).__name__}: {e}"),
            status_code=400,
        )
    cooldown = result.get("cooldown_required_days", 0)
    msg = f"Applied profile <strong>{profile}</strong>; wrote lock {result['lock_version']}."
    if cooldown:
        msg += f" <strong>{cooldown}-day cooldown applies</strong> for loosened thresholds."
    return HTMLResponse(
        tmpl.risk_page(controls.risk_profile_show(), flash=msg)
    )


@app.get("/halt", response_class=HTMLResponse)
def halt_get():
    return tmpl.halt_page(controls.status_snapshot())


@app.post("/halt/halt")
def halt_post(reason: str = Form(...)):
    operator = os.environ.get("USER", "operator")
    out = controls.halt(reason=reason, operator=operator)
    return HTMLResponse(
        tmpl.halt_page(controls.status_snapshot(),
                       flash=f"Halted: seq={out['ledger_seq']}, active={out['active']}")
    )


@app.post("/halt/resume")
def resume_post(reason: str = Form(...)):
    operator = os.environ.get("USER", "operator")
    out = controls.resume(reason=reason, operator=operator)
    return HTMLResponse(
        tmpl.halt_page(controls.status_snapshot(),
                       flash=f"Resumed: seq={out['ledger_seq']}, active={out['active']}")
    )


@app.get("/strategy", response_class=HTMLResponse)
def strategy_get():
    strategies = controls.strategy_list()
    return tmpl.strategy_page(strategies)


@app.post("/strategy/submit")
def strategy_submit(
    name: str = Form(...),
    description: str = Form(...),
    mode: str = Form("draft"),
):
    operator = os.environ.get("USER", "operator")
    try:
        result = controls.strategy_submit(
            name=name, description=description, mode=mode, operator=operator,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("strategy_submit failed")
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return HTMLResponse(tmpl.strategy_result_page(result))


__all__ = ["app"]
