"""Email notification for every unblock-committee debate.

Operator wants visibility into ALL debates (not just overrides) — what
ticker, what the gate rejected, what the 3 reviewers argued, and what
the judge decided. Sent once per debate via Gmail SMTP, journaled to
state.db like every other email.

Designed to be called from any debate site (wheel hook today;
stock_scanner / crypto_scanner hooks when those land in 5.5/5.6).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from trading_bot.shared.config import Settings, load_config
from trading_bot.email_fill import Email
from trading_bot.email_log import send_logged
from trading_bot.email_sender import EmailSender
from trading_bot.unblock_debate import UnblockVerdict


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebateEmailContext:
    asset_class: str           # "wheel" | "stock" | "crypto"
    symbol: str
    block_reason: str
    overage_ratio: float       # 0.0 = at cap, 0.5 = 50% over
    candidate_score: float     # 0-10
    proposal_summary: str      # multi-line proposal text
    fundamentals: str          # candidate context block
    operational_context: str   # account state block
    verdict: Optional[UnblockVerdict]  # None when fail-closed


def _verdict_pill_html(rec: str, conf: str) -> str:
    color = {"place": "#10b981", "reject": "#ef4444"}.get(rec, "#94a3b8")
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


def build_unblock_debate_email(ctx: DebateEmailContext) -> Email:
    """Render the full debate as a single email so the operator can see
    exactly what the committee considered for this ticker.
    """
    v = ctx.verdict

    if v is None:
        verdict_html = (
            '<span style="display:inline-block;padding:4px 12px;'
            'background:#475569;color:#f1f5f9;border-radius:12px;'
            'font-weight:600;font-family:monospace;font-size:13px;">'
            'FAIL-CLOSED'
            '</span>'
        )
        verdict_label = "fail_closed"
    else:
        verdict_html = _verdict_pill_html(v.recommendation, v.confidence)
        verdict_label = v.recommendation

    subject = (
        f"[UNBLOCK·{verdict_label.upper()}] {ctx.asset_class}/{ctx.symbol} — "
        f"{ctx.block_reason[:60]}"
    )

    summary_table = (
        f'<table style="width:100%;border-collapse:collapse;margin:12px 0;'
        f'font-family:Inter,system-ui,Arial,sans-serif;font-size:13px;">'
        f'<tr><td style="padding:6px 0;color:#94a3b8;width:30%;">verdict</td>'
        f'<td style="padding:6px 0;">{verdict_html}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">asset_class</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{_escape(ctx.asset_class)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">symbol</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;font-weight:600;">{_escape(ctx.symbol)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">block_reason</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{_escape(ctx.block_reason)}</td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">overage_ratio</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{ctx.overage_ratio:.2f}  '
        f'<span style="color:#94a3b8;">(0=at cap, 0.5=50% over)</span></td></tr>'
        f'<tr><td style="padding:6px 0;color:#94a3b8;">candidate_score</td>'
        f'<td style="padding:6px 0;color:#f1f5f9;font-family:monospace;">{ctx.candidate_score:.2f} / 10</td></tr>'
        f'</table>'
    )

    judge_block = ""
    if v is not None:
        judge_block = _block("judge reasoning", v.reason or "(empty)")

    debater_blocks = ""
    if v is not None:
        debater_blocks = (
            _block("aggressive (argues for override)", v.aggressive_text)
            + _block("conservative (argues for respecting cap)", v.conservative_text)
            + _block("neutral (balanced read)", v.neutral_text)
        )
    else:
        debater_blocks = _block(
            "fail-closed",
            "The debate could not produce a valid structured verdict — "
            "credentials missing, budget exhausted, SDK error, or judge "
            "schema mismatch. Original gate rejection stands. Check the "
            "daemon stderr log for the specific error.",
        )

    body_html = (
        f'<div style="background:#020617;color:#f1f5f9;padding:24px;'
        f'font-family:Inter,system-ui,Arial,sans-serif;font-size:14px;'
        f'line-height:1.55;">'
        f'<div style="font-size:18px;font-weight:600;margin-bottom:6px;">'
        f'Unblock committee · {_escape(ctx.symbol)}</div>'
        f'<div style="font-size:13px;color:#94a3b8;margin-bottom:18px;">'
        f'Adversarial debate over a deterministic risk-cap rejection. '
        f'Verdict logged to <code style="color:#f1f5f9;">unblock_debate_runs</code> '
        f'whether or not the order was placed.'
        f'</div>'
        f'{summary_table}'
        f'{judge_block}'
        f'{_block("proposed order", ctx.proposal_summary)}'
        f'{_block("candidate fundamentals", ctx.fundamentals)}'
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


def send_debate_email(ctx: DebateEmailContext, *,
                      settings: Settings | None = None,
                      cfg=None) -> bool:
    """Send the debate email. Returns True on success, False on any error.

    Email failures must NEVER crash the wheel scan — wrapped in
    try/except.
    """
    try:
        settings = settings or Settings()
        if cfg is None:
            from pathlib import Path
            cfg = load_config(Path("strategy/config.yaml"))
        email = build_unblock_debate_email(ctx)
        sender = EmailSender(
            user=settings.gmail_user,
            app_password=settings.gmail_app_password,
            to=cfg.email.to,
        )
        send_logged(
            sender=sender, subject=email.subject, html_body=email.html_body,
            kind="unblock_debate", recipient=cfg.email.to,
        )
        return True
    except Exception as e:
        log.warning("send_debate_email failed for %s/%s: %s",
                    ctx.asset_class, ctx.symbol, e)
        return False
