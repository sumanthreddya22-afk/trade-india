"""Build the Trading Bot project documentation PDF.

Pipeline:
    1. load JSON content (content/*.json)
    2. render the Jinja2 base template into a single HTML string
    3. open it in headless Chromium (Playwright)
    4. wait for Mermaid to finish rendering every diagram
    5. emit a Letter-size PDF with running header + page numbers

Output: docs/project_overview_v2.pdf
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from render import ROOT, render_html


REPO_ROOT = ROOT.parent.parent
OUT_PDF = REPO_ROOT / "docs" / "project_overview_v2.pdf"


HEADER_HTML = """
<div style="font-family: Inter, sans-serif; font-size: 8pt; color: #475569;
            width: 100%; padding: 0 .55in; display: flex; justify-content: space-between;">
  <span>Trading Bot — Project Overview</span>
  <span style="color: #0EA5E9;">v2.0 · 2026-04-29</span>
</div>
"""

FOOTER_HTML = """
<div style="font-family: Inter, sans-serif; font-size: 8pt; color: #475569;
            width: 100%; padding: 0 .55in; display: flex; justify-content: space-between;">
  <span>claude-opus-4-7 pinned · Alpaca paper</span>
  <span>Page <span class="pageNumber"></span> / <span class="totalPages"></span></span>
</div>
"""


async def html_to_pdf(html: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 1600})
        page = await context.new_page()

        # Capture console errors so we know if Mermaid blew up.
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: page_errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None)

        # Write HTML to disk so Chromium loads it from a file:// origin —
        # set_content gives the page an opaque origin that blocks file:// fetches.
        html_path = ROOT / "_preview.html"
        html_path.write_text(html)
        await page.goto(html_path.as_uri(), wait_until="domcontentloaded")
        # Wait for Mermaid to finish rendering all diagrams (sync flag set in base.html.j2).
        try:
            await page.wait_for_function("window.__mermaidDone === true", timeout=120_000)
        except Exception as e:
            print(f"!! Mermaid sync timed out: {e}", file=sys.stderr)

        # Print to PDF
        await page.emulate_media(media="print")
        await page.pdf(
            path=str(out),
            format="Letter",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0.85in", "bottom": "0.7in", "left": "0.55in", "right": "0.55in"},
            display_header_footer=True,
            header_template=HEADER_HTML,
            footer_template=FOOTER_HTML,
        )

        if page_errors:
            print("!! Console errors during render:")
            for err in page_errors[:10]:
                print(f"   {err}")

        await browser.close()


def main() -> int:
    print("==> rendering HTML")
    html = render_html()
    print(f"   {len(html):,} bytes of HTML")

    debug_html = ROOT / "_preview.html"
    debug_html.write_text(html)
    print(f"   wrote debug copy → {debug_html}")

    print("==> launching Chromium and writing PDF")
    asyncio.run(html_to_pdf(html, OUT_PDF))
    size_kb = OUT_PDF.stat().st_size / 1024
    print(f"==> done: {OUT_PDF} ({size_kb:,.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
