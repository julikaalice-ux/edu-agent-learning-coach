"""General scene contracts, offline deployment assets, and frame controls."""

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent"
sys.path.insert(0, str(SOURCE))
from learning_coach.adapters import scenes
from learning_coach.adapters.visuals import render_visual
from learning_coach.db.artifacts import ArtifactStore
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from tests.test_visuals import Page


def scene_spec(topic="光传播的位置示意", actor="光点", shape="ellipse"):
    return {
        "kind": "scene",
        "title": topic,
        "learning_goal": f"观察{actor}在三个步骤中的位置。",
        "interaction_hint": "切换步骤，对比同一对象的位置。",
        "check_question": "它朝哪个方向移动？",
        "limitations": "位置变化仅用于控制器测试，不代表实测速度或真实时间。",
        "frames": [
            {
                "title": title,
                "explanation": f"{actor}处于{title}。",
                "elements": [
                    {
                        "id": "actor",
                        "shape": shape,
                        "x": x,
                        "y": 35,
                        "width": 14,
                        "height": 18,
                        "color": "accent",
                        "opacity": 1,
                        "label": actor,
                    },
                    {
                        "id": "direction",
                        "shape": "arrow",
                        "x1": 10,
                        "y1": 75,
                        "x2": 85,
                        "y2": 75,
                        "color": "light",
                        "opacity": 1,
                        "label": "",
                    },
                    {
                        "id": "caption",
                        "shape": "text",
                        "x": 10,
                        "y": 10,
                        "width": 80,
                        "height": 15,
                        "color": "ink",
                        "text": title,
                    },
                ],
            }
            for title, x in (("起点", 10), ("中间位置", 35), ("终点", 60))
        ],
    }


class SceneContractTests(unittest.TestCase):
    def test_unrelated_topics_use_the_same_renderer_and_fixed_scripts(self):
        documents = []
        for spec in (scene_spec(), scene_spec("队列任务流转", "任务", "rect")):
            Draft202012Validator(VISUAL_SCHEMA).validate(spec)
            page = Page(render_visual(spec))
            self.assertEqual(json.loads(page.scripts[0]["text"])["spec"], spec)
            documents.append(page)
        self.assertEqual(
            [s["text"] for s in documents[0].scripts[1:]],
            [s["text"] for s in documents[1].scripts[1:]],
        )

    def test_schema_rejects_arbitrary_code_resources_and_unbounded_scenes(self):
        spec = scene_spec()
        invalid = [
            {**spec, "javascript": "alert(1)"},
            {**spec, "asset_url": "https://example.invalid/a.js"},
            {**spec, "frames": spec["frames"][:1]},
            {**spec, "frames": spec["frames"] * 3},
        ]
        for changes in (
            {"shape": "svg"},
            {"x": 101},
            {"opacity": -1},
            {"color": "url(evil)"},
        ):
            bad = deepcopy(spec)
            bad["frames"][0]["elements"][0].update(changes)
            invalid.append(bad)
        bad = deepcopy(spec)
        bad["frames"][0]["elements"] *= 11
        invalid.append(bad)
        for bad in invalid:
            with self.subTest(spec=bad):
                with self.assertRaises(ValueError):
                    render_visual(bad)

    def test_semantic_geometry_rejects_out_of_bounds_duplicate_and_unreadable_labels(
        self,
    ):
        cases = []
        bad = scene_spec()
        bad["frames"][0]["elements"][0]["x"] = 95
        cases.append((bad, "scene_out_of_bounds"))
        bad = scene_spec()
        bad["frames"][0]["elements"][0]["x"] = float("nan")
        cases.append((bad, "scene_nonfinite_number"))
        bad = scene_spec()
        bad["frames"][0]["elements"].append(deepcopy(bad["frames"][0]["elements"][0]))
        cases.append((bad, "scene_duplicate_id"))
        bad = scene_spec()
        copy = deepcopy(bad["frames"][0]["elements"][0])
        copy["id"] = "overlapping"
        bad["frames"][0]["elements"].append(copy)
        cases.append((bad, "scene_labels_overlap"))
        bad = scene_spec()
        bad["frames"][0]["elements"][0]["height"] = 1
        # Too long even for the room a name gets outside a small shape.
        bad["frames"][0]["elements"][0]["label"] = "很长很长的标签文字" * 5
        cases.append((bad, "scene_label_does_not_fit"))
        bad = scene_spec()
        bad["frames"][0]["elements"][1]["x2"] = 10
        cases.append((bad, "scene_zero_length_line"))
        for bad, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    render_visual(bad)

    def test_render_errors_name_the_frame_and_elements(self):
        from learning_coach.domain.scene_contracts import SceneError, scene_layout
        bad = scene_spec()
        copy = deepcopy(bad["frames"][1]["elements"][0])
        copy["id"] = "overlapping"
        bad["frames"][1]["elements"].append(copy)
        with self.assertRaises(SceneError) as caught:
            scene_layout(bad)
        self.assertEqual(str(caught.exception), "scene_labels_overlap")
        self.assertEqual(caught.exception.where, "第 2 帧的 actor 和 overlapping")
        bad = scene_spec()
        bad["frames"][2]["elements"][0]["x"] = 95
        with self.assertRaises(SceneError) as caught:
            scene_layout(bad)
        self.assertEqual((str(caught.exception), caught.exception.where), ("scene_out_of_bounds", "第 3 帧的 actor"))

    def test_a_small_shapes_name_goes_below_it_instead_of_a_redraw(self):
        from learning_coach.domain.scene_contracts import Y_SCALE, scene_layout
        spec = scene_spec()
        particle = spec["frames"][0]["elements"][0]
        particle.update(width=3, height=6, label="质子")
        fitted = scene_layout(spec)[0]["actor"]
        self.assertTrue(fitted["outside"])
        self.assertGreater(fitted["cy"], (particle["y"] + particle["height"]) * Y_SCALE)
        self.assertIn("!box.outside", Page(render_visual(spec)).scripts[-1]["text"])

    def test_field_marks_are_a_background_other_labels_may_sit_on(self):
        from learning_coach.domain.scene_contracts import scene_layout
        spec = scene_spec()
        for frame in spec["frames"]:
            frame["elements"].insert(0, {"id": "field", "shape": "text", "x": 0, "y": 30, "width": 100,
                                         "height": 30, "color": "neutral", "text": "×　×　×　×　×　×　×　×"})
        layout = scene_layout(spec)
        self.assertTrue(layout[0]["field"]["background"])
        self.assertNotIn("background", layout[0]["actor"])
        page = Page(render_visual(spec))
        self.assertIn("box.background?art:labels", page.scripts[-1]["text"])
        # Ordinary words in the same place still count as overlapping.
        spec["frames"][1]["elements"][0]["text"] = "磁场方向垂直纸面向里"
        with self.assertRaisesRegex(ValueError, "scene_labels_overlap"):
            scene_layout(spec)

    def test_offline_page_isolates_model_text_and_authorizes_exact_fixed_scripts(self):
        normal = Page(render_visual(scene_spec()))
        spec = scene_spec()
        spec["title"] = (
            '</script><script src="https://example.invalid/x.js">evil()</script>'
        )
        spec["frames"][0]["elements"][2]["text"] = '<img src=x onerror="evil()">'
        page = Page(render_visual(spec))
        self.assertEqual(len(page.scripts), 3)
        self.assertEqual(json.loads(page.scripts[0]["text"])["spec"], spec)
        self.assertEqual(page.scripts[0]["attrs"]["type"], "application/json")
        self.assertNotIn("<", page.scripts[0]["text"])
        self.assertEqual(
            [s["text"] for s in page.scripts[1:]],
            [s["text"] for s in normal.scripts[1:]],
        )
        self.assertFalse(any("src" in script["attrs"] for script in page.scripts))
        self.assertFalse(any(tag in ("iframe", "img", "link") for tag, _ in page.tags))
        self.assertFalse(
            any(
                name.lower().startswith("on")
                for _, attrs in page.tags
                for name in attrs
            )
        )
        self.assertNotIn("@import", page.styles[0]["text"])
        self.assertNotIn("url(", page.styles[0]["text"])
        policy = next(
            attrs["content"]
            for tag, attrs in page.tags
            if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
        )
        directives = dict(part.strip().split(" ", 1) for part in policy.split(";"))
        digests = [
            "'sha256-"
            + base64.b64encode(hashlib.sha256(s["text"].encode()).digest()).decode()
            + "'"
            for s in page.scripts[1:]
        ]
        self.assertEqual(directives["script-src"].split(), digests)
        self.assertEqual(directives["default-src"], "'none'")
        self.assertTrue(all("sourceMappingURL" not in s["text"] for s in page.scripts))

    def test_changed_vendor_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            vendor = Path(temporary)
            (vendor / "svg.min.js").write_bytes(b"untrusted replacement")
            with patch.object(scenes, "VENDOR", vendor):
                with self.assertRaisesRegex(ValueError, "invalid_visual_dependency"):
                    render_visual(scene_spec())


class SceneDeploymentTests(unittest.TestCase):
    def test_source_package_alone_contains_every_required_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "learning_coach"
            shutil.copytree(
                SOURCE / "learning_coach",
                package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            self.assertTrue((package / "adapters/vendor/SVGJS-LICENSE.txt").is_file())
            script = (
                "import json,sys; from learning_coach.adapters.visuals import render_visual; "
                "document=render_visual(json.load(sys.stdin)); "
                "print(json.dumps({'ready':'scene-data' in document,'length':len(document)}))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps(scene_spec()),
                cwd=temporary,
                text=True,
                capture_output=True,
                check=True,
                timeout=20,
                env={
                    **os.environ,
                    "PYTHONPATH": temporary,
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            rendered = json.loads(result.stdout)
            self.assertTrue(rendered["ready"])
            self.assertGreater(rendered["length"], 1000)

    def test_real_artifact_store_preserves_scene_across_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            spec = scene_spec()
            store = ArtifactStore(temporary, renderer=render_visual)
            prepared = store.prepare("learner", spec)
            self.assertIsNone(store.get(prepared["token"]))
            store.commit("learner", prepared)
            restarted = ArtifactStore(temporary, renderer=render_visual)
            document = restarted.get(prepared["token"])
            self.assertEqual(document, prepared["html"])
            self.assertEqual(
                json.loads(Page(document).scripts[0]["text"])["spec"], spec
            )
            restarted.delete_session("learner")
            self.assertIsNone(restarted.get(prepared["token"]))


@unittest.skipUnless(shutil.which("node"), "Node is required to execute scene controls")
class SceneInteractionTests(unittest.TestCase):
    def test_step_slider_play_pause_and_finish_change_the_production_scene(self):
        for spec in (scene_spec(), scene_spec("队列任务流转", "任务", "rect")):
            with self.subTest(topic=spec["title"]):
                data = json.loads(Page(render_visual(spec)).scripts[0]["text"])
                result = subprocess.run(
                    [shutil.which("node"), str(ROOT / "tests/scene_harness.cjs")],
                    input=json.dumps(
                        {
                            "data": data,
                            "script": (scenes.ASSETS / "scene.js").read_text(),
                            "markup": (scenes.ASSETS / "scene.html").read_text(),
                        }
                    ),
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                states = json.loads(result.stdout)
                initial = states["initial"]
                next = states["next"]
                slider = states["slider"]
                self.assertEqual(initial["frame"], "0")
                self.assertTrue(initial["previousDisabled"])
                self.assertEqual(next["frame"], "1")
                self.assertNotEqual(initial["art"], next["art"])
                self.assertEqual(next["title"], spec["frames"][1]["title"])
                self.assertEqual(next["selectedSteps"], ["false", "true", "false"])
                self.assertEqual(states["previous"], initial)
                self.assertEqual(slider["frame"], "2")
                self.assertTrue(slider["nextDisabled"])
                self.assertEqual(slider["slider"], "2")
                self.assertEqual(states["restart"]["frame"], "0")
                self.assertEqual(states["restart"]["playing"], "true")
                self.assertEqual(states["advanced"]["frame"], "1")
                self.assertEqual(states["paused"], states["stillPaused"])
                self.assertEqual(states["paused"]["activeTimers"], 0)
                self.assertEqual(states["finished"]["frame"], "2")
                self.assertEqual(states["finished"]["playing"], "false")
                self.assertEqual(states["finished"]["activeTimers"], 0)
                self.assertEqual(states["firstStep"], initial)


if __name__ == "__main__":
    unittest.main()
