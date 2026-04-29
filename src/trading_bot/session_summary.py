"""End-of-day session summary — generates a "what went well / wrong /
improve" review from the data the digest already has access to.

Pure-function design: takes a DigestContext-like dict, returns three
lists of strings. No I/O, easily testable, deterministic.

Rules are intentionally simple and rule-based (no LLM round-trip).
The bot has plenty of structured data; the value is in surfacing the
right pieces, not in synthesizing fresh prose."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SessionReview:
    went_well: list[str]
    went_wrong: list[str]
    improvements: list[str]


def _pct(n: Decimal | float) -> str:
    n = float(n)
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.2f}%"


def _money(n: Decimal | float) -> str:
    n = float(n)
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


def review_session(ctx) -> SessionReview:
    """Inspect a DigestContext and produce a three-bucket review.

    Each bullet is a single sentence with a concrete number. We avoid
    generic praise ("system is working") in favor of specific
    observations the operator can act on."""
    well: list[str] = []
    wrong: list[str] = []
    improve: list[str] = []

    # ---------------------------------------------------------------
    # 1) P&L direction + magnitude
    # ---------------------------------------------------------------
    pnl = float(ctx.realized_pnl) + float(ctx.unrealized_pnl)
    starting = float(ctx.starting_equity) or 1.0
    pnl_pct = (pnl / starting) * 100 if starting else 0.0

    if pnl_pct > 0.5:
        well.append(f"Day P&L positive: {_pct(pnl_pct)} ({_money(pnl)})")
    elif pnl_pct < -0.5:
        wrong.append(f"Day P&L negative: {_pct(pnl_pct)} ({_money(pnl)})")
    else:
        well.append(f"Day P&L flat: {_pct(pnl_pct)} ({_money(pnl)})")

    # Risk-cap proximity
    if ctx.daily_loss_pct < -ctx.daily_loss_cap_pct * 0.75:
        wrong.append(
            f"Daily loss {_pct(ctx.daily_loss_pct)} approaching cap "
            f"-{ctx.daily_loss_cap_pct}% (75%+ of limit)"
        )
    if ctx.weekly_loss_pct < -ctx.weekly_loss_cap_pct * 0.75:
        wrong.append(
            f"Weekly loss {_pct(ctx.weekly_loss_pct)} approaching cap "
            f"-{ctx.weekly_loss_cap_pct}% (75%+ of limit)"
        )
    if ctx.drawdown_pct > ctx.drawdown_cap_pct * 0.5:
        wrong.append(
            f"Drawdown at {ctx.drawdown_pct:.1f}% — over half of "
            f"{ctx.drawdown_cap_pct}% cap"
        )

    # ---------------------------------------------------------------
    # 2) Today's trades
    # ---------------------------------------------------------------
    trades = list(getattr(ctx, "trades", []) or [])
    if not trades:
        # No trades is informational — could be either: bot was disciplined
        # (good) or missed signals (bad). Flag for operator awareness.
        improve.append(
            "Zero trades placed today — verify intel gates / opportunities "
            "are firing as expected"
        )
    else:
        buys = [t for t in trades if str(getattr(t, "side", "")).upper() == "BUY"]
        well.append(f"{len(trades)} trade decisions executed ({len(buys)} buys)")

    # Closed trades win-rate (last 7 days)
    closed = list(getattr(ctx, "closed_trades_7d", []) or [])
    if closed:
        wins = sum(1 for t in closed if float(t.get("pnl", 0)) > 0)
        win_rate = wins / len(closed) if closed else 0.0
        if win_rate >= 0.6:
            well.append(
                f"7d win rate {win_rate*100:.0f}% ({wins}/{len(closed)} closed)"
            )
        elif win_rate <= 0.4:
            wrong.append(
                f"7d win rate {win_rate*100:.0f}% ({wins}/{len(closed)} closed) "
                f"— below 40% target"
            )

    # ---------------------------------------------------------------
    # 3) Errors / daemon health
    # ---------------------------------------------------------------
    errors = list(getattr(ctx, "errors", []) or [])
    if errors:
        wrong.append(
            f"{len(errors)} runtime errors logged today "
            f"(see Errors section)"
        )
    else:
        well.append("Zero runtime errors logged")

    blips = int(getattr(ctx, "daemon_blips", 0) or 0)
    if blips > 0:
        wrong.append(f"Daemon stalled {blips} time(s) today (auto-recovered)")

    # ---------------------------------------------------------------
    # 4) Schedule audit
    # ---------------------------------------------------------------
    audit_warnings = list(getattr(ctx, "schedule_audit_warnings", []) or [])
    if audit_warnings:
        wrong.append(
            f"{len(audit_warnings)} cron job(s) didn't fire as scheduled today"
        )

    # ---------------------------------------------------------------
    # 5) Regime / Vol environment
    # ---------------------------------------------------------------
    if ctx.regime == "risk_off":
        improve.append(
            "Risk-off regime active — entries auto-suspended; verify "
            "exit/reverse logic on existing positions"
        )
    elif ctx.regime == "trending_down":
        improve.append(
            "Trending-down regime — defensive allocation; consider tightening "
            "trailing stops on remaining longs"
        )

    if ctx.vix is not None:
        if ctx.vix > ctx.vol_threshold_pct:
            improve.append(
                f"VIX {ctx.vix:.1f} above {ctx.vol_threshold_pct:.0f} "
                f"threshold — momentum lane sensitivity reduced; consider "
                f"sizing down or raising IV-rank floor for wheel"
            )
        elif ctx.vix < 13.0:
            improve.append(
                f"VIX low at {ctx.vix:.1f} — wheel premiums thin; "
                f"consider widening DTE range or raising delta target"
            )

    # ---------------------------------------------------------------
    # 6) Wheel-specific findings
    # ---------------------------------------------------------------
    open_cycles = list(getattr(ctx, "wheel_open_cycles", []) or [])
    if open_cycles:
        well.append(f"{len(open_cycles)} wheel cycle(s) open")
    if ctx.wheel_pnl_mtd > 0:
        well.append(f"Wheel MTD P&L: {_money(ctx.wheel_pnl_mtd)}")
    elif ctx.wheel_pnl_mtd < 0:
        wrong.append(f"Wheel MTD P&L: {_money(ctx.wheel_pnl_mtd)}")
    if ctx.wheel_collateral_pct > 18.0:
        improve.append(
            f"Wheel collateral at {ctx.wheel_collateral_pct:.1f}% of equity — "
            f"approaching options cap; sector cap may start blocking new CSPs"
        )

    # ---------------------------------------------------------------
    # 7) Sentiment / Watchlist signals
    # ---------------------------------------------------------------
    sentiment = list(getattr(ctx, "sentiment_scores", []) or [])
    if sentiment:
        very_negative = [s for s in sentiment if float(s.get("score", 0)) < -0.5]
        if very_negative:
            improve.append(
                f"{len(very_negative)} watchlist names with sentiment < -0.5 "
                f"— momentum lane will skip these via sentiment_floor"
            )

    movers = list(getattr(ctx, "watchlist_movers", []) or [])
    if movers and not trades:
        improve.append(
            f"{len(movers)} watchlist movers but no trades — review whether "
            f"intel gates (earnings/macro/sentiment) blocked entries"
        )

    # ---------------------------------------------------------------
    # 8) Pending promotions
    # ---------------------------------------------------------------
    promotions = list(getattr(ctx, "pending_promotions", []) or [])
    if promotions:
        improve.append(
            f"{len(promotions)} strategy promotion(s) pending operator review"
        )

    # ---------------------------------------------------------------
    # 9) Always-on safety: at least ONE positive observation
    # ---------------------------------------------------------------
    if not well:
        well.append(
            "Trading session completed — no critical breaches of risk caps"
        )
    if not improve:
        improve.append(
            "No specific improvements flagged; system performing as configured"
        )

    return SessionReview(
        went_well=well, went_wrong=wrong, improvements=improve,
    )
