"""HTML render helper. Loads JSON content + renders the Jinja2 base template."""
from __future__ import annotations

import json
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape, StrictUndefined


ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"
TEMPLATES_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"


def load_content() -> dict:
    """Load every JSON file under content/ keyed by stem."""
    ctx: dict = {}
    for jf in sorted(CONTENT_DIR.glob("*.json")):
        ctx[jf.stem] = json.loads(jf.read_text())
    return ctx


def render_html() -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(disabled_extensions=("j2",), default_for_string=False, default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    ctx = load_content()
    ctx["asset_root_url"] = ASSETS_DIR.as_uri()
    tpl = env.get_template("base.html.j2")
    return tpl.render(**ctx)


if __name__ == "__main__":
    out = ROOT / "_preview.html"
    out.write_text(render_html())
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
