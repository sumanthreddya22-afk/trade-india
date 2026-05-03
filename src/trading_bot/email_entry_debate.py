"""Email transcript for every entry-committee debate.

Operator wants to see the FULL debate (all three reviewer texts + judge
reasoning) for every BUY signal that fired the entry committee, AND
whether the order was actually placed. Mirror of
:mod:`trading_bot.email_unblock_debate`; differs only in:

  * Subject prefix ``[ENTRY·…]``
  * Outcome field (placed / place_failed / skipped) — the unblock email
    only carries the *verdict*; the entry email carries what actually
    happened so the operator can reconcile "the judge said place" with
    "the order is/isn't in Alpaca".
  * No ``block_reason`` / ``overage_ratio`` (those are unblock-only); the
    entry email shows ``intel_score`` + ``signal_reason`` + ``regime``
    instead.

Sent once per debate that ran. Fail-soft and over-cap paths skip the
email — there's no transcript when the debate never happened. Those
paths already queue a ``daemon_critical`` alert separately.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Literal, Optional

from trading_bot.shared.config import Settings, load_config
from trading_bot.email_fill import Email
from trading_bot.email_log import send_logged
from trading_bot.email_sender import EmailSender
from trading_bot.entry_debate import EntryDebateVerdict


log = logging.getLogger(__name__)


# What actually happened to the order after the debate completed.
# - placed       → verdict=place AND alpaca.place_order_with_stop_loss succeeded
# - place_failed → verdict=place BUT alpaca raised (debate said yes; order didn't reach broker)
# - skipped      → verdict=skip; no order submitted
EntryOutcome = Literal["placed", "place_failed", "skipped"]


@dataclass(frozen=True)
class EntryDebateEmailContext:
    asset_class: str           # "stock" | "crypto"
    symbol: str
    intel_score: float | None
    signal_reason: str
    regime: str
    proposal_summary: str      # multi-line proposal text
    indicators: str
    operational_context: str
    verdict: EntryDebateVerdict   # required — over_cap/fail-soft don't email
    outcome: EntryOutcome
    entry_order_id: str = ""     # populated when outcome == "placed"
    place_error: str = ""        # populated when outcome == "place_failed"


def _outcome_pill_html(outcome: EntryOutcome) -> str:
    color = {
        "placed":       "#10b981",   # green
        "place_failed": "#f59e0b",   # amber — judge said yes but broker rejected
        "skipped":      "#ef4444",   # red
    }.get(outcome, "#94a3b8")
    label = {
        "placed":       "ORDER PLACED",
        "place_failed": "PLACE FAILED",
        "skipped":      "ORDER SKIPPED",
    }.get(outcome, outcome.upper())
    return (
        f'<span style="display:inline-block;padding:4px 12px;'
        f'background:{color};color:#0a1322;border-radius:12px;'
        f'font-weight:600;font-family:monospace;font-size:13px;">'
        f'{label}'
        f'</span>'
    )


def _verdict_pill_html(rec: str, conf: str) -> str:
    color = {"place": "#10b981", "skip": "#ef4444"}.get(rec, "#94a3b8")
    return (
        f'<span style="display:inline-block;padding:4px 12px;'
        f'background:{color};color:#0a1322;border-radius:12px;'
        f'font-weight:600;font-family:monospace;font-size:13px;">'
        f'{rec.upper()} ({conf})'
        f'</span>'
    )


def _block(title: str, body: str) -> str:
    return (
        f'<div style="margin:16px 0;">'
        f'<div style="font-size:11px;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:0.08em;margin-bottom:6px;font-weight:600;">{title}</div>'
        f'<pre style="margin:0;padding:12px;background:#0a1322;'
        f'border:1px solid #26334a;border-radius:6px;color:#f1f5f9;'
        f"font-family:'SF Mono','JetBrains Mono',Menlo,Consolas,monospace;"
        f'font-size:12px;line-height:1.55;white-space:pre-wrap;'
        f'word-break:break-word;">{_escape(body)}</pre>'
        f'</div>'
    )


def _escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_entry_debate_email(ctx: EntryDebateEmailContext) -> Email:
    """Render the full debate as a single email so the operator can see
    exactly what the committee considered AND what actually happened.
    """
    v = ctx.verdict
    intel_str = (
        f"{ctx.intel_score:.2f}" if ctx.intel_score is not None else "(none)"
    )

    subject = (
        f"[ENTRY·{ctx.outcome.upper()}] {ctx.asset_class}/{ctx.symbol} — "
        f"judge: {v.recommendation}({v.confidence})"
    )

    summary_table = (
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0;'
        f'font-family:Inter,system-ui,Arial,sans-serif;font-size:13px;">'
        f'<tr><td style="padding:6px 0;color:#94a3b8;width:30%;">outcome</td>'
        f'<td style="padding:6px 0;">{_outcome_pill_html(ctx.outcome)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">judge verdict</td>'
        f'<td style="padding:6px 0;">{_verdict_pill_html(v.recommendation, v.confidence)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">asset_class</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{_escape(ctx.asset_class)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">symbol</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;font-weight:600;">{_escape(ctx.symbol)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">intel_score</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{intel_str}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">signal_reason</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{_escape(ctx.signal_reason)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">regime</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{_escape(ctx.regime)}</td></tr>'
    )
    if ctx.outcome == "placed" and ctx.entry_order_id:
        summary_table += (
            f'<tr><td style="padding:6px 0;color:#94a3b8;">entry_order_id</td>'
            f'<td style="padding:6px 0;color:#10b981;font-family:monospace;">{_escape(ctx.entry_order_id)}</td></tr>'
        )
    if ctx.outcome == "place_failed" and ctx.place_error:
        summary_table += (
            f'<tr><td style="padding:6px 0;color:#94a3b8;">broker_error</td>'
            f'<td style="padding:6px 0;color:#f59e0b;font-family:monospace;">{_escape(ctx.place_error[:240])}</td></tr>'
        )
    summary_table += '</table>'

    judge_block = _block("judge reasoning", v.reason or "(empty)")

    debater_blocks = (
        _block("aggressive (argues for placing)", v.aggressive_text)
        + _block("conservative (argues for skipping)", v.conservative_text)
        + _block("neutral (balanced read)", v.neutral_text)
    )

    body_html = (
        f'<div style="background:#020617;color:#f1f5f9;padding:24px;'
        f'font-family:Inter,system-ui,Arial,sans-serif;font-size:14px;'
        f'line-height:1.55;">'
        f'<div style="font-size:18px;font-weight:600;margin-bottom:6px;">'
        f'Entry committee · {_escape(ctx.symbol)}</div>'
        f'<div style="font-size:13px;color:#94a3b8;margin-bottom:18px;">'
        f'Pre-trade adversarial debate. Verdict logged to '
        f'<code style="color:#f1f5f9;">entry_debate_runs</code> '
        f'whether or not the order was placed.'
        f'</div>'
        f'{summary_table}'
        f'{judge_block}'
        f'{_block("proposed order", ctx.proposal_summary)}'
        f'{_block("indicators", ctx.indicators)}'
        f'{_block("operational context", ctx.operational_context)}'
        f'<div style="margin-top:20px;font-size:12px;color:#94a3b8;'
        f'text-transform:uppercase;letter-spacing:0.08em;font-weight:600;">'
        f'Reviewer transcripts'
        f'</div>'
        f'{debater_blocks}'
        f'<div style="margin-top:24px;padding-top:12px;border-top:1px solid #1e293b;'
        f'font-size:11px;color:#64748b;">'
        f'sent {dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}'
        f'</div>'
        f'</div>'
    )

    return Email(subject=subject, html_body=body_html)


def send_entry_debate_email(
    ctx: EntryDebateEmailContext,
    *,
    settings: Settings | None = None,
    cfg=None,
) -> bool:
    """Send the entry-debate email. Returns True on success, False on any error.

    Email failures must NEVER crash the scan — wrapped in try/except.
    """
    try:
        settings = settings or Settings()
        if cfg is None:
            from pathlib import Path
            cfg = load_config(Path("strategy/config.yaml"))
        email = build_entry_debate_email(ctx)
        sender = EmailSender(
            user=settings.gmail_user,
            app_password=settings.gmail_app_password,
            to=cfg.email.to,
        )
        send_logged(
            sender=sender, subject=email.subject, html_body=email.html_body,
            kind="entry_debate", recipient=cfg.email.to,
        )
        return True
    except Exception as e:
        log.warning("send_entry_debate_email failed for %s/%s: %s",
                    ctx.asset_class, ctx.symbol, e)
        return False
