"""Tests for the shared email visual shell — gradient_header, kpi_card,
sparkline_svg, progress_bar, pulse_dot, severity_pill, footer, render_shell."""
import pytest


def test_render_shell_includes_brand_bar_and_status_dot():
    from trading_bot.email_shell import render_shell
    html = render_shell(
        title="Daily Digest",
        status="ok",
        timestamp_et="2026-04-28 22:00 ET",
        body_sections=["<p>body</p>"],
    )
    assert "Daily Digest" in html
    assert "linear-gradient" in html  # brand bar
    assert "#10b981" in html or "rgb(16,185,129)" in html  # green pulse
    assert "<p>body</p>" in html
    assert "2026-04-28 22:00 ET" in html


def test_render_shell_amber_for_warn():
    from trading_bot.email_shell import render_shell
    html = render_shell(title="x", status="warn", timestamp_et="t",
                        body_sections=[])
    assert "#fbbf24" in html


def test_render_shell_red_for_bad():
    from trading_bot.email_shell import render_shell
    html = render_shell(title="x", status="bad", timestamp_et="t",
                        body_sections=[])
    assert "#fb7185" in html


def test_kpi_card_renders_label_value_delta():
    from trading_bot.email_shell import kpi_card
    html = kpi_card(label="Equity", value="$14,953", delta="-0.21%",
                    delta_kind="bad")
    assert "Equity" in html
    assert "$14,953" in html
    assert "-0.21%" in html


def test_sparkline_svg_renders_polyline():
    from trading_bot.email_shell import sparkline_svg
    html = sparkline_svg([100.0, 102.5, 101.0, 103.7, 102.9],
                         width=120, height=32)
    assert "<svg" in html
    assert "polyline" in html
    assert "stroke" in html


def test_sparkline_handles_empty_list():
    from trading_bot.email_shell import sparkline_svg
    # Empty data → minimal placeholder, no crash
    html = sparkline_svg([], width=120, height=32)
    assert "<svg" in html
    assert "polyline" not in html  # nothing to plot


def test_progress_bar_clamps_value():
    from trading_bot.email_shell import progress_bar
    html = progress_bar(value_pct=120.0, color="#fb7185", label="x")
    assert "width:100%" in html or "width: 100%" in html


def test_progress_bar_below_zero():
    from trading_bot.email_shell import progress_bar
    html = progress_bar(value_pct=-5.0, color="#10b981", label="x")
    assert "width:0%" in html or "width: 0%" in html


def test_pulse_dot_color_by_status():
    from trading_bot.email_shell import pulse_dot
    assert "#10b981" in pulse_dot("ok")
    assert "#fbbf24" in pulse_dot("warn")
    assert "#fb7185" in pulse_dot("bad")


def test_severity_pill_kinds():
    from trading_bot.email_shell import severity_pill
    assert "long" in severity_pill("long", "good").lower()
    assert "16,185,129" in severity_pill("ok", "good") or "#34d399" in severity_pill("ok", "good")


def test_section_renders_glyph_and_title():
    from trading_bot.email_shell import section
    html = section(title="Positions", glyph="📈", body="<p>x</p>")
    assert "📈" in html
    assert "Positions" in html
    assert "<p>x</p>" in html


def test_data_table_zebra_rows():
    from trading_bot.email_shell import data_table
    html = data_table(
        headers=["Sym", "Qty", "Px"],
        rows=[["AAPL", "10", "$200.00"], ["MSFT", "5", "$400.00"]],
    )
    assert "AAPL" in html
    assert "MSFT" in html


def test_footer_includes_version_and_git_sha():
    from trading_bot.email_shell import footer
    html = footer(version="v1.2", git_sha="abc1234",
                  dashboard_url="http://localhost:8000")
    assert "v1.2" in html
    assert "abc1234" in html
    assert "http://localhost:8000" in html
