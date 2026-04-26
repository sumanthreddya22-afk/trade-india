"""Render backtest run results to markdown."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_bot.backtest.metrics import BacktestMetrics, SliceMetrics
from trading_bot.backtest.simulator import BacktestRunResult


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _fmt_pf(v: float | None) -> str:
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    return f"{v:.2f}"


def _fmt_decimal(v: Decimal | None, sign: bool = False) -> str:
    if v is None:
        return "—"
    s = "+" if sign and v > 0 else ""
    return f"{s}${v:,.2f}"


def _row(s: SliceMetrics) -> tuple[str, ...]:
    return (
        str(s.n),
        f"{s.win_rate_pct:.1f}%" if s.win_rate_pct is not None else "—",
        _fmt_pf(s.profit_factor),
        f"{s.sharpe_daily_ann:.2f}" if s.sharpe_daily_ann is not None else "—",
        _fmt_pct(s.max_drawdown_pct),
        f"{s.avg_hold_days:.1f}d" if s.avg_hold_days is not None else "—",
        _fmt_decimal(s.total_pnl),
    )


def render_markdown(result: BacktestRunResult, metrics: BacktestMetrics) -> str:
    lines: list[str] = []
    lines.append("# Backtest Results")
    lines.append("")
    lines.append(f"- **Run id:** `{result.run_id}`")
    lines.append(f"- **Generated:** {result.generated_at.isoformat(timespec='seconds')}")
    lines.append(f"- **Range:** {result.from_date} → {result.to_date}")
    lines.append(f"- **Symbols ({len(result.symbols)}):** {', '.join(result.symbols)}")
    lines.append(f"- **Strategies:** {', '.join(result.strategies_used)}")
    lines.append(
        f"- **Equity:** {result.starting_equity:.0f} → "
        f"{result.ending_equity:,.0f} "
        f"({(result.ending_equity / result.starting_equity - 1) * 100:+.2f}%)"
    )
    lines.append(f"- **Halted days:** {result.halted_days}")
    lines.append(f"- **Skipped (risk):** {result.skipped_by_risk}")
    lines.append(f"- **Skipped (no bars):** {result.skipped_no_bars}")
    lines.append("")

    # Headline
    lines.append("## Headline")
    lines.append("")
    headers = ["", "Trades", "Win %", "PF", "Sharpe", "Max DD", "Avg hold", "Total P&L"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    lines.append("| **Overall** | " + " | ".join(_row(metrics.overall)) + " |")
    for strat, s in sorted(metrics.per_strategy.items()):
        lines.append(f"| {strat} | " + " | ".join(_row(s)) + " |")
    lines.append("")

    # Per-strategy × regime
    lines.append("## Per-strategy × regime")
    lines.append("")
    if metrics.per_strategy_regime:
        sub_headers = ["Strategy", "Regime", "Trades", "Win %", "PF", "Sharpe", "Max DD", "Avg hold", "P&L"]
        lines.append("| " + " | ".join(sub_headers) + " |")
        lines.append("|" + "|".join(["---"] * len(sub_headers)) + "|")
        for (strat, regime), s in sorted(metrics.per_strategy_regime.items()):
            lines.append(f"| {strat} | {regime} | " + " | ".join(_row(s)) + " |")
        lines.append("")
    else:
        lines.append("_No trades to slice._")
        lines.append("")

    # Asset class
    if metrics.per_asset_class:
        lines.append("## Per-asset class")
        lines.append("")
        sub_headers = ["Asset class", "Trades", "Win %", "PF", "Sharpe", "Max DD", "Avg hold", "P&L"]
        lines.append("| " + " | ".join(sub_headers) + " |")
        lines.append("|" + "|".join(["---"] * len(sub_headers)) + "|")
        for ac, s in sorted(metrics.per_asset_class.items()):
            lines.append(f"| {ac} | " + " | ".join(_row(s)) + " |")
        lines.append("")

    # Acceptance gate
    lines.append("## Acceptance gate")
    lines.append("")
    dom = metrics.dominant_regime
    lines.append(f"Dominant regime by trade count: **{dom}**")
    lines.append("")
    dom_strats = {(s, r): m for (s, r), m in metrics.per_strategy_regime.items() if r == dom}
    if not dom_strats:
        lines.append("_No trades in any regime — gate cannot be evaluated._")
    else:
        lines.append("| Strategy | Trades ≥ 30 | PF ≥ 1.0 | Sharpe ≥ 0.5 | Verdict |")
        lines.append("|---|---|---|---|---|")
        any_fail = False
        for (strat, _), m in sorted(dom_strats.items()):
            t_ok = m.n >= 30
            pf = m.profit_factor if m.profit_factor is not None else 0.0
            pf_ok = pf is float("inf") or pf >= 1.0
            sh_ok = m.sharpe_daily_ann is not None and m.sharpe_daily_ann >= 0.5
            verdict = "✓ pass" if (t_ok and pf_ok and sh_ok) else "✗ revisit rules"
            if not (t_ok and pf_ok and sh_ok):
                any_fail = True
            lines.append(
                f"| {strat} | {'✓' if t_ok else '✗'} ({m.n}) | "
                f"{'✓' if pf_ok else '✗'} ({_fmt_pf(m.profit_factor)}) | "
                f"{'✓' if sh_ok else '✗'} ({m.sharpe_daily_ann if m.sharpe_daily_ann is not None else '—'}) | "
                f"{verdict} |"
            )
        lines.append("")
        if any_fail:
            lines.append("> **At least one strategy fails the gate. Per Plan 5b spec, do not "
                         "silently ship 5c/5d. Inspect the failing slice and adjust strategy.py "
                         "before further plans.**")
        else:
            lines.append("> **All gates pass. Proceed to Plan 5c (exit hardening) with empirical baseline.**")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_markdown_report(
    result: BacktestRunResult,
    metrics: BacktestMetrics,
    path: Path | str,
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_markdown(result, metrics))
