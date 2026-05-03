"""Phase D — DebateOutcomeAnalyzerRole.

Tier-3 nightly role that:

  1. Calls ``lesson_loop.aggregate_outcomes`` to read the last 14 days
     of debate runs joined with closed-trade outcomes.
  2. Sends the structured report to the Performance Attribution Analyst
     persona (single LLM call) for narrative synthesis + candidate prompt
     edits.
  3. Persists the result to ``debate_lessons`` so future debate briefs
     can inject it under "RECENT LESSONS".

Fail-soft: any LLM error writes a "no-lesson" row with an explanation
note rather than crashing — the absence of a fresh lesson is informative
itself (operator alert path can pick it up).

Sequential: aggregation queries run one source at a time; the LLM call
fires after aggregation completes.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy.orm import Session

from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun


log = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MAX_TOKENS = 1200


class DebateOutcomeAnalyzerRole(BaseRole):
    name = "debate_outcome_analyzer"
    tier = 3
    process = "lab"
    job_description = (
        "Nightly: joins debate verdicts with closed-trade outcomes and "
        "writes a one-page lessons summary that feeds future debates."
    )
    sla_seconds = 5 * 60
    upstream_roles = ["position_monitor", "intel_ingestor"]
    downstream_roles = []

    def __init__(self, *, engine, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
        super().__init__(engine=engine)
        self._lookback_days = lookback_days

    def _do_work(self, ctx) -> dict:
        from trading_bot import lesson_loop
        report = lesson_loop.aggregate_outcomes(
            self.engine, lookback_days=self._lookback_days,
        )
        # Bypass the LLM entirely when there's nothing to summarise
        if report.n_trades_closed == 0 and report.n_entry_debates == 0:
            return {
                "n_trades_closed": 0,
                "skipped_reason": "no debates or trades in lookback window",
                "wrote_lesson": False,
            }

        # Build the analyst prompt user-message
        user_message = _format_report_for_analyst(report)

        # Sequential single-LLM call — analyst persona summarises + drafts
        # candidate edits. Fail-soft: any error writes a placeholder row
        # so the audit log records that the analyzer ran.
        summary_text = ""
        candidate_edits: list[dict] = []
        prompt_version = ""
        try:
            from trading_bot.anthropic_client import (
                AnthropicCredsMissingError, BudgetExceededError,
                default_architect_model,
            )
            from trading_bot.mailbox_backed_client import (
                MailboxBackedClient, MailboxRouting,
            )
            from trading_bot.personas import lesson_analyst
            try:
                client = MailboxBackedClient(
                    role_name=self.name,
                    model=default_architect_model(),
                    engine=self.engine,
                    routing=MailboxRouting(
                        enabled=True, timeout_seconds=600.0, model_class="judge",
                    ),
                )
                resp = client.complete(
                    system=lesson_analyst.PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                    max_tokens=DEFAULT_MAX_TOKENS,
                )
                summary_text = (resp.text or "").strip()
                prompt_version = f"lesson_analyst={lesson_analyst.VERSION}"
                # Extract candidate edits heuristically from the summary
                # (analyst prose includes a "CANDIDATE EDITS" section)
                candidate_edits = _extract_candidate_edits(summary_text)
            except AnthropicCredsMissingError:
                summary_text = "(no anthropic creds — analyzer skipped)"
            except BudgetExceededError:
                summary_text = "(budget halt — analyzer skipped)"
            except Exception as e:  # noqa: BLE001
                log.warning("debate_outcome_analyzer: LLM error: %s", e)
                summary_text = f"(analyzer LLM error: {e})"
        except Exception as e:  # noqa: BLE001
            # Outermost guard — even import failures shouldn't crash the role
            log.error("debate_outcome_analyzer: outer error: %s", e)
            summary_text = f"(analyzer outer error: {e})"

        lesson_loop.write_lesson(
            self.engine,
            report=report,
            summary_text=summary_text,
            candidate_edits=candidate_edits,
            prompt_version=prompt_version,
        )
        return {
            "n_trades_closed": report.n_trades_closed,
            "n_entry_debates": report.n_entry_debates,
            "n_unblock_debates": report.n_unblock_debates,
            "n_hold_debates": report.n_hold_debates,
            "wrote_lesson": True,
            "summary_chars": len(summary_text),
            "n_candidate_edits": len(candidate_edits),
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
        return (
            "analyzer_runs",
            float(count),
            f"{count} debate-outcome-analyzer runs in last {lookback_days}d",
        )


def _format_report_for_analyst(report) -> str:
    """Convert an OutcomeReport into the human-readable user message the
    analyst persona expects."""
    lines: list[str] = []
    lines.append(f"AGGREGATE STATS (last {report.lookback_days} days):")
    lines.append(f"  trades_closed: {report.n_trades_closed}")
    lines.append(f"  entry_debates: {report.n_entry_debates}")
    lines.append(f"  unblock_debates: {report.n_unblock_debates}")
    lines.append(f"  hold_debates: {report.n_hold_debates}")
    lines.append(
        f"  overall_place_winrate: "
        f"{_pct(report.overall_place_winrate)}"
    )
    lines.append(
        f"  overall_skip_winrate: "
        f"{_pct(report.overall_skip_winrate)}"
    )
    lines.append("")
    lines.append("PER-VERDICT WINRATE:")
    if report.per_verdict_winrate:
        for verdict, stats in sorted(report.per_verdict_winrate.items()):
            lines.append(
                f"  {verdict}: n={stats['n']}, "
                f"winrate={stats['winrate']*100:.0f}%, "
                f"avg_pnl_pct={stats['avg_pnl_pct']:+.2f}"
            )
    else:
        lines.append("  (no closed trades with verdicts)")
    lines.append("")
    lines.append("PER-SOURCE WINRATE (entries that mentioned each source):")
    if report.per_source_winrate:
        # Sort by n desc then winrate desc
        for src, stats in sorted(
            report.per_source_winrate.items(),
            key=lambda kv: (-kv[1]["n"], -kv[1]["winrate"]),
        ):
            lines.append(
                f"  {src}: n={stats['n']}, "
                f"winrate={stats['winrate']*100:.0f}%, "
                f"avg_pnl_pct={stats['avg_pnl_pct']:+.2f}"
            )
    else:
        lines.append("  (no source attribution available)")
    lines.append("")
    lines.append("LOSING TRADES (sample, worst-first):")
    if report.losing_patterns:
        for losing in report.losing_patterns:
            lines.append(
                f"  - {losing.get('symbol','?')} "
                f"(verdict={losing.get('verdict','?')}, "
                f"pnl={losing.get('pnl_pct',0):.2f}%, "
                f"intel_score={losing.get('intel_score')})"
            )
            jr = (losing.get('judge_reason') or '').replace('\n', ' ')
            if jr:
                lines.append(f"    judge: {jr[:280]}")
    else:
        lines.append("  (no losing trades in sample)")
    lines.append("")
    lines.append("SHADOW-TRACKED SKIPPED TRADES (judge said 'skip'):")
    if report.shadow_skips:
        for sk in report.shadow_skips:
            lines.append(
                f"  - {sk.get('symbol','?')} "
                f"(intel_score={sk.get('intel_score')}, "
                f"regime={sk.get('regime','?')})"
            )
            jr = (sk.get('judge_reason') or '').replace('\n', ' ')
            if jr:
                lines.append(f"    judge: {jr[:280]}")
    else:
        lines.append("  (no skipped trades in sample)")
    return "\n".join(lines)


def _pct(x: float | None) -> str:
    return "(none)" if x is None else f"{x*100:.0f}%"


def _extract_candidate_edits(summary_text: str) -> list[dict]:
    """Heuristic: find lines under a 'CANDIDATE EDITS' header and return
    each as {'edit': line, 'accepted': False}. Operator review flips
    'accepted' true and applies the edit to the persona prompts.
    """
    if not summary_text:
        return []
    lines = summary_text.splitlines()
    in_section = False
    out: list[dict] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.upper().startswith("CANDIDATE EDIT"):
            in_section = True
            continue
        if in_section:
            # Stop at next ALL-CAPS section header
            if s == s.upper() and s.endswith(":"):
                break
            # Strip bullets / numbering
            cleaned = s.lstrip("-*0123456789. ").strip()
            if cleaned:
                out.append({"edit": cleaned, "accepted": False})
    return out
