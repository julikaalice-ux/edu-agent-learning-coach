"""Render general, model-designed storyboards with pinned, offline SVG.js."""

import base64
import hashlib
import html
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from learning_coach.domain.scene_contracts import scene_layout
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA

ASSETS = Path(__file__).with_name("scene_assets")
VENDOR = Path(__file__).with_name("vendor")
SVGJS_SHA256 = "0490b8cf4b27f75ceddf1ae462d1142eb3b86f9600e7251175b905c6d1c9057c"


def render_scene(spec: dict) -> str:
    if (not isinstance(spec, dict) or spec.get("kind") != "scene"
            or not Draft202012Validator(VISUAL_SCHEMA).is_valid(spec)):
        raise ValueError("invalid_scene")
    layout = scene_layout(spec)
    library = (VENDOR / "svg.min.js").read_bytes()
    if hashlib.sha256(library).hexdigest() != SVGJS_SHA256:
        raise ValueError("invalid_visual_dependency")
    library_text = library.decode("utf-8").split("//# sourceMappingURL=", 1)[0]
    script = (ASSETS / "scene.js").read_text(encoding="utf-8")
    styles = (ASSETS / "scene.css").read_text(encoding="utf-8")
    body = (ASSETS / "scene.html").read_text(encoding="utf-8")
    data = json.dumps({"spec": spec, "layout": layout}, ensure_ascii=True, allow_nan=False)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
              for s in (library_text, script)]
    policy = "default-src 'none'; script-src " + " ".join(hashes) + "; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    title = html.escape(spec["title"], quote=True)
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
            '<title>' + title + '</title><style>' + styles + '</style></head><body>'
            + body.replace("{{TITLE}}", title)
            + '<script type="application/json" id="scene-data">' + data + '</script>'
            + '<script>' + library_text + '</script><script>' + script + '</script></body></html>')
