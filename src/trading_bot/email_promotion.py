"""Strategy Promotion email — sent once per lab promotion."""
from __future__ import annotations

from typing import Any

from trading_bot.email_fill import Email
from trading_bot.email_shell import (
    render_shell, section, data_table, severity_pill, footer,
    _GOOD_LIGHT, _BAD, _WARN, _TEXT_PRIMARY, _ACCENT,
)


def _params_diff_rows(new_params: dict[str, Any],
                      old_params: dict[str, Any] | None) -> list[list[str]]:
    keys = sorted(set(new_params.keys()) | set((old_params or {}).keys()))
    rows = []
    for k in keys:
        new_val = new_params.get(k)
        old_val = (old_params or {}).get(k)
        if new_val == old_val:
            rows.append([k, str(old_val), str(new_val), "—"])
        else:
            arrow = (
                f'<span style="color:{_GOOD_LIGHT}">→</span>'
                if old_val is None or
                   (isinstance(new_val, (int, float)) and isinstance(old_val, (int, float)) and new_val > old_val)
                else f'<span style="color:{_BAD}">→</span>'
            )
            rows.append([k, str(old_val), str(new_val), arrow])
    return rows


def build_promotion_email(*, promo: dict[str, Any],
                          prev: dict[str, Any] | None) -> Email:
    subject = (
        f"Strategy Promoted · {promo['version']} · "
        f"fitness {promo['fitness_at_promotion']:.2f}"
    )

    body_sections = []

    # Summary
    body_sections.append(section(
        title="Summary", glyph="🧪",
        body=(
            f'<table style="font-family:inherit;color:{_TEXT_PRIMARY};font-size:13px">'
            f'<tr><td><b>Version</b></td><td>{promo["version"]}</td></tr>'
            f'<tr><td><b>Template</b></td><td>{promo["template"]}</td></tr>'
            f'<tr><td><b>Git SHA</b></td><td>{promo["git_sha"]}</td></tr>'
            f'<tr><td><b>Fitness</b></td><td>{promo["fitness_at_promotion"]:.3f}</td></tr>'
            f'<tr><td><b>Promoted at</b></td><td>{promo["promoted_at"]:%Y-%m-%d %H:%M UTC}</td></tr>'
            f'</table>'
        ),
    ))

    # Params diff
    body_sections.append(section(
        title="Params Diff", glyph="◆",
        body=data_table(
            headers=["Param", "Old", "New", ""],
            rows=_params_diff_rows(promo.get("params", {}),
                                   (prev or {}).get("params") if prev else None),
        ),
    ))

    # Risk caps diff
    body_sections.append(section(
        title="Risk Caps", glyph="🛡️",
        body=data_table(
            headers=["Cap", "Old", "New", ""],
            rows=_params_diff_rows(promo.get("risk_caps", {}),
                                   (prev or {}).get("risk_caps") if prev else None),
        ),
    ))

    # Watch first 24h
    body_sections.append(section(
        title="Watch first 24h", glyph="👁️",
        body=(
            f'<p style="color:{_TEXT_PRIMARY};font-size:13px">'
            f'The next daily digest will track first-24h validation: '
            f'scans engaged, entries fired, near-misses. If zero entries '
            f'after 24h, the digest will flag the strategy as too restrictive.</p>'
        ),
        severity="info",
    ))

    body_sections.append(footer(version=promo.get("version", "—"),
                                git_sha=promo.get("git_sha", "—")))

    return Email(
        subject=subject,
        html_body=render_shell(
            title="Strategy Promotion",
            status="ok",
            timestamp_et=promo["promoted_at"].strftime("%a, %b %d %Y · %H:%M UTC"),
            body_sections=body_sections,
        ),
    )
