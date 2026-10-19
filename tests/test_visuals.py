"""Bounded specs, markup isolation, and the artifact acceptance boundary."""

import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator


import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from learning_coach.adapters import visuals
from learning_coach.db import artifacts

EXAMPLES = [
    {"kind": "comparison", "title": "蒸发与沸腾", "left_label": "蒸发", "right_label": "沸腾",
     "rows": [{"aspect": "观察位置", "left": "液体表面", "right": "液体内部和表面"}]},
    {"kind": "process", "title": "解题步骤", "steps": [
        {"title": "理解条件", "description": "找到已知条件。"},
        {"title": "选择方法", "description": "说明所选方法的依据。"},
    ]},
    {"kind": "flashcards", "title": "复习卡", "cards": [{"front": "2 + 3 = ?", "back": "5"}]},
    {"kind": "linear_function", "title": "直线怎样变化", "slope": 1, "intercept": 0,
     "x_min": -10, "x_max": 10},
]


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.scripts = []
        self.styles = []
        self._active = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script":
            self.scripts.append({"attrs": attrs, "text": ""})
            self._active = self.scripts[-1]
        elif tag == "style":
            self.styles.append({"attrs": attrs, "text": ""})
            self._active = self.styles[-1]

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._active = None

    def handle_data(self, data):
        if self._active is not None:
            self._active["text"] += data


class RenderTests(unittest.TestCase):
    def test_all_spec_types_are_valid_self_contained_and_use_fixed_script(self):
        Draft202012Validator.check_schema(visuals.VISUAL_SCHEMA)
        scripts = []
        for spec in EXAMPLES:
            with self.subTest(kind=spec["kind"]):
                Draft202012Validator(visuals.VISUAL_SCHEMA).validate(spec)
                page = Page(visuals.render_visual(spec))
                self.assertEqual(len(page.scripts), 2)
                self.assertEqual(json.loads(page.scripts[0]["text"]), spec)
                self.assertEqual(page.scripts[0]["attrs"]["type"], "application/json")
                scripts.append(page.scripts[1]["text"])
                self.assertTrue(all("src" not in attrs and "href" not in attrs for _, attrs in page.tags))
                self.assertFalse(any(key.lower().startswith("on") for _, attrs in page.tags for key in attrs))
                self.assertNotIn("url(", page.styles[0]["text"])
                self.assertIn("textContent", scripts[-1])
                self.assertIn("addEventListener('click'", scripts[-1])
                self.assertIn("addEventListener('input'", scripts[-1])
                for unsafe in ("innerHTML", "outerHTML", "eval(", "fetch(", "XMLHttpRequest", "document.write"):
                    self.assertNotIn(unsafe, scripts[-1])
        self.assertTrue(all(script == scripts[0] for script in scripts))

    def test_injection_payloads_remain_data_not_markup_or_scripts(self):
        payloads = [
            '<script>alert("x")</script>',
            '</script><script src="https://evil.invalid/x.js">evil()</script>',
            '\" autofocus onfocus=\"alert(1)\" data-x=\"',
            '</style><style>body{background:url(https://evil.invalid/a)}</style>',
            '<img src=x onerror="alert(1)">',
        ]
        normal = Page(visuals.render_visual(EXAMPLES[2]))
        for payload in payloads:
            spec = {"kind": "flashcards", "title": payload,
                    "cards": [{"front": payload, "back": payload}]}
            with self.subTest(payload=payload):
                page = Page(visuals.render_visual(spec))
                self.assertEqual(len(page.scripts), 2)
                self.assertEqual(len(page.styles), 1)
                self.assertEqual(page.scripts[1]["text"], normal.scripts[1]["text"])
                self.assertEqual(page.styles[0]["text"], normal.styles[0]["text"])
                self.assertEqual(json.loads(page.scripts[0]["text"]), spec)
                self.assertNotIn("<", page.scripts[0]["text"])
                self.assertTrue(all(tag != "img" for tag, _ in page.tags))
                self.assertFalse(any(key.lower().startswith("on") for _, attrs in page.tags for key in attrs))

    def test_csp_authorizes_exact_fixed_script(self):
        page = Page(visuals.render_visual(EXAMPLES[0]))
        policy = next(attrs["content"] for tag, attrs in page.tags
                      if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        digest = base64.b64encode(hashlib.sha256(page.scripts[1]["text"].encode()).digest()).decode()
        self.assertIn("script-src 'sha256-" + digest + "'", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("script-src 'unsafe-inline'", policy)

    def test_schema_rejects_code_fields_wrong_types_and_excessive_content(self):
        invalid = [
            {**EXAMPLES[0], "html": "<script>alert(1)</script>"},
            {**EXAMPLES[1], "steps": []},
            {**EXAMPLES[2], "cards": EXAMPLES[2]["cards"] * 21},
            {**EXAMPLES[3], "slope": True},
            {**EXAMPLES[3], "slope": 11},
            {**EXAMPLES[3], "x_min": -21},
            {**EXAMPLES[0], "title": "x" * 121},
            {**EXAMPLES[0], "title": "  "},
        ]
        for spec in invalid:
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    visuals.render_visual(spec)

    def test_function_requires_finite_numbers_and_ordered_range(self):
        for changes in ({"slope": float("nan")}, {"intercept": float("inf")},
                        {"x_min": 10, "x_max": 10}, {"x_min": 11, "x_max": 10}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    visuals.render_visual({**EXAMPLES[3], **changes})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = artifacts.ArtifactStore(self.root, renderer=visuals.render_visual)

    def test_prepare_does_not_publish_and_commit_survives_restart(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        artifact = self.store.prepare("session-one", EXAMPLES[0])
        after = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(set(artifact), {"token", "url", "title", "html"})
        self.assertRegex(artifact["token"], r"^[A-Za-z0-9_-]{32}$")
        self.assertEqual(artifact["url"], "http://localhost:8088/artifacts/" + artifact["token"])
        self.assertIsNone(self.store.get(artifact["token"]))
        self.store.commit("session-one", artifact)
        self.assertEqual(self.store.get(artifact["token"]), artifact["html"])
        restarted = artifacts.ArtifactStore(self.root, renderer=visuals.render_visual)
        self.assertEqual(restarted.get(artifact["token"]), artifact["html"])
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))
        self.assertNotIn(b"session-one", (self.root / "index.sqlite3").read_bytes())

    def test_session_deletion_is_isolated_and_persistent(self):
        first = self.store.prepare("one", EXAMPLES[1])
        second = self.store.prepare("two", EXAMPLES[2])
        self.store.commit("one", first)
        self.store.commit("two", second)
        restarted = artifacts.ArtifactStore(self.root, renderer=visuals.render_visual)
        restarted.delete_session("one")
        self.assertIsNone(restarted.get(first["token"]))
        self.assertFalse((self.root / (first["token"] + ".html")).exists())
        self.assertEqual(restarted.get(second["token"]), second["html"])
        self.assertEqual(artifacts.ArtifactStore(self.root, renderer=visuals.render_visual).get(second["token"]), second["html"])

    def test_cannot_commit_other_session_or_modified_html(self):
        artifact = self.store.prepare("one", EXAMPLES[0])
        with self.assertRaises(ValueError):
            self.store.commit("two", artifact)
        bad = {**artifact, "html": "<script>evil()</script>"}
        with self.assertRaises(ValueError):
            self.store.commit("one", bad)
        self.assertIsNone(self.store.get(artifact["token"]))
        self.store.commit("one", artifact)
        self.assertEqual(self.store.get(artifact["token"]), artifact["html"])

    def test_delete_session_also_revokes_pending_artifacts(self):
        artifact = self.store.prepare("one", EXAMPLES[2])
        self.store.delete_session("one")
        with self.assertRaises(ValueError):
            self.store.commit("one", artifact)

    def test_failed_session_unlink_keeps_index_for_retry_and_revokes_pending(self):
        artifact = self.store.prepare("one", EXAMPLES[0])
        other = self.store.prepare("two", EXAMPLES[1])
        pending = self.store.prepare("one", EXAMPLES[2])
        self.store.commit("one", artifact)
        self.store.commit("two", other)
        path = self.root / (artifact["token"] + ".html")
        with patch.object(Path, "unlink", side_effect=OSError("simulated unlink failure")):
            with self.assertRaises(OSError):
                self.store.delete_session("one")
        self.assertTrue(path.exists())
        with self.store._connection() as db:
            self.assertIsNotNone(db.execute("SELECT token FROM artifacts WHERE token = ?",
                                           (artifact["token"],)).fetchone())
        with self.assertRaises(ValueError):
            self.store.commit("one", pending)
        restarted = artifacts.ArtifactStore(self.root, renderer=visuals.render_visual)
        restarted.delete_session("one")
        self.assertFalse(path.exists())
        self.assertIsNone(restarted.get(artifact["token"]))
        self.assertEqual(restarted.get(other["token"]), other["html"])

    def test_invalid_tokens_never_resolve_paths(self):
        for token in ("../index.sqlite3", "/etc/passwd", "a" * 31, "a" * 33, "a" * 32 + "\n",
                      "%2e%2e%2f", "a" * 16 + "/" + "b" * 15, "", None):
            with self.subTest(token=token):
                self.assertIsNone(self.store.get(token))
        self.assertIsNone(self.store.get("a" * 32))

    def test_expiry_is_fixed_24_hours_and_removes_file(self):
        with patch.object(artifacts.time, "time", return_value=1000):
            artifact = self.store.prepare("one", EXAMPLES[3])
            self.store.commit("one", artifact)
        with patch.object(artifacts.time, "time", return_value=1000 + self.store.TTL_SECONDS - 1):
            self.assertEqual(self.store.get(artifact["token"]), artifact["html"])
        with patch.object(artifacts.time, "time", return_value=1000 + self.store.TTL_SECONDS + 1):
            restarted = artifacts.ArtifactStore(self.root, renderer=visuals.render_visual)
            self.assertIsNone(restarted.get(artifact["token"]))
            self.assertFalse((self.root / (artifact["token"] + ".html")).exists())

    def test_failed_expiry_unlink_keeps_index_for_next_cleanup(self):
        with patch.object(artifacts.time, "time", return_value=1000):
            artifact = self.store.prepare("one", EXAMPLES[3])
            self.store.commit("one", artifact)
        path = self.root / (artifact["token"] + ".html")
        with patch.object(artifacts.time, "time", return_value=1000 + self.store.TTL_SECONDS + 1):
            with patch.object(Path, "unlink", side_effect=OSError("simulated unlink failure")):
                with self.assertRaises(OSError):
                    self.store.get(artifact["token"])
            self.assertTrue(path.exists())
            with self.store._connection() as db:
                self.assertIsNotNone(db.execute("SELECT token FROM artifacts WHERE token = ?",
                                               (artifact["token"],)).fetchone())
            restarted = artifacts.ArtifactStore(self.root, renderer=visuals.render_visual)
            self.assertFalse(path.exists())
            self.assertIsNone(restarted.get(artifact["token"]))

    def test_failed_atomic_write_does_not_publish_or_leave_temporary_file(self):
        artifact = self.store.prepare("one", EXAMPLES[0])
        with patch.object(artifacts.os, "replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                self.store.commit("one", artifact)
        self.assertIsNone(self.store.get(artifact["token"]))
        self.assertFalse(any(path.suffix in (".tmp", ".html") for path in self.root.iterdir()))

    def test_symlink_cannot_be_served(self):
        artifact = self.store.prepare("one", EXAMPLES[2])
        self.store.commit("one", artifact)
        path = self.root / (artifact["token"] + ".html")
        path.unlink()
        path.symlink_to(self.root / "index.sqlite3")
        self.assertIsNone(self.store.get(artifact["token"]))

    def test_public_url_has_no_credentials_query_or_fragment(self):
        for base in ("javascript:alert(1)", "https://user:password@example.com", "https://example.com?token=x",
                     "https://example.com/#x", "https://example.com/ bad"):
            with self.subTest(base=base):
                with self.assertRaises(ValueError):
                    artifacts.ArtifactStore(self.root, base, renderer=visuals.render_visual)
        store = artifacts.ArtifactStore(self.root, "https://example.com/base/", renderer=visuals.render_visual)
        artifact = store.prepare("one", EXAMPLES[0])
        self.assertEqual(artifact["url"], "https://example.com/base/artifacts/" + artifact["token"])


if __name__ == "__main__":
    unittest.main()
