"""Tests for the diagram visual kind: schema, rendering, escaping, pipeline."""

import re
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.domain.contracts import validate  # noqa: E402
from learning_coach.db.memory import LearningStore  # noqa: E402
from learning_coach.service.pipeline import LearningPipeline  # noqa: E402
from learning_coach.adapters.visuals import VISUAL_SCHEMA, render_visual
from learning_coach.db.artifacts import ArtifactStore  # noqa: E402
from test_pipeline import FakeArtifacts, FakeResearch, FakeRunner, RESPONSE_SCHEMA, messages, plan  # noqa: E402


def photosynthesis_spec():
    return {
        "kind": "diagram",
        "title": "光合作用的要素关系",
        "nodes": [
            {"id": "a", "label": "原料：二氧化碳和水", "x": 10, "y": 50},
            {"id": "b", "label": "场所：叶绿体", "x": 50, "y": 50},
            {"id": "c", "label": "产物：有机物和氧气", "x": 90, "y": 50},
        ],
        "edges": [
            {"from": "a", "to": "b", "label": "在光照下进入"},
            {"from": "b", "to": "c"},
        ],
    }


class DiagramSchemaTests(unittest.TestCase):
    def test_valid_diagram_passes_schema(self):
        validate(photosynthesis_spec(), VISUAL_SCHEMA)

    def test_diagram_without_edges_passes_schema(self):
        spec = photosynthesis_spec()
        spec["edges"] = []
        validate(spec, VISUAL_SCHEMA)

    def test_diagram_missing_node_field_fails_schema(self):
        spec = photosynthesis_spec()
        del spec["nodes"][0]["x"]
        with self.assertRaises(Exception):
            validate(spec, VISUAL_SCHEMA)

    def test_diagram_coordinate_out_of_range_fails_schema(self):
        spec = photosynthesis_spec()
        spec["nodes"][0]["x"] = 101
        with self.assertRaises(Exception):
            validate(spec, VISUAL_SCHEMA)

    def test_diagram_edge_extra_property_fails_schema(self):
        spec = photosynthesis_spec()
        spec["edges"][0]["weight"] = 3
        with self.assertRaises(Exception):
            validate(spec, VISUAL_SCHEMA)


class DiagramRenderTests(unittest.TestCase):
    def test_renders_static_svg_page(self):
        document = render_visual(photosynthesis_spec())
        self.assertIn("<svg", document)
        self.assertIn("原料：二氧化碳和水", document)
        self.assertIn("在光照下进入", document)
        self.assertIn("光合作用的要素关系", document)
        # Static page: no scripts, no dynamic JSON data block.
        self.assertNotIn("<script", document)

    def test_boxes_fit_their_words_and_never_overlap(self):
        # Nodes placed on top of each other, with long words, as a designer sometimes does.
        spec = {"kind": "diagram", "title": "重叠的节点", "nodes": [
            {"id": "a", "label": "蓝光更容易向各个方向散射开来，所以天空看起来偏蓝", "x": 50, "y": 50},
            {"id": "b", "label": "从天空各个方向都更容易接收到蓝光", "x": 52, "y": 50},
            {"id": "c", "label": "时针位置：3与4正中", "x": 50, "y": 52},
            {"id": "d", "label": "4", "x": 51, "y": 51}],
            "edges": [{"from": "a", "to": "b", "label": "散射到各个方向"}, {"from": "c", "to": "d"}]}
        document = render_visual(spec)
        boxes = [tuple(float(value) for value in match) for match in
                 re.findall(r'<rect x="(-?[\d.]+)" y="(-?[\d.]+)" width="([\d.]+)" height="([\d.]+)"', document)]
        self.assertEqual(len(boxes), 4)
        for i, (x1, y1, w1, h1) in enumerate(boxes):
            for x2, y2, w2, h2 in boxes[i + 1:]:
                self.assertFalse(x1 < x2 + w2 and x2 < x1 + w1 and y1 < y2 + h2 and y2 < y1 + h1)
        # A long name wraps onto more than one line, inside its box.
        self.assertGreaterEqual(document.count("<tspan"), 6)
        text = "".join(re.findall(r"<tspan[^>]*>([^<]*)</tspan>", document))
        for node in spec["nodes"]:
            self.assertIn(node["label"].replace("，", ""), text.replace("，", ""))

    def test_node_label_script_is_escaped(self):
        spec = photosynthesis_spec()
        spec["nodes"][0]["label"] = '<script>alert(1)</script>'
        document = render_visual(spec)
        self.assertNotIn("<script>", document)
        self.assertIn("&lt;script&gt;", document)

    def test_dangling_edge_raises(self):
        spec = photosynthesis_spec()
        spec["edges"].append({"from": "a", "to": "zzz"})
        with self.assertRaises(ValueError):
            render_visual(spec)

    def test_duplicate_node_id_raises(self):
        spec = photosynthesis_spec()
        spec["nodes"][1]["id"] = "a"
        with self.assertRaises(ValueError):
            render_visual(spec)

    def test_invalid_spec_rejected_before_branch(self):
        with self.assertRaises(ValueError):
            render_visual({"kind": "diagram", "title": "t", "nodes": [], "edges": []})


class DiagramPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")
        self.research = FakeResearch()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_diagram_visual_commits_svg_artifact(self):
        self.store.set_enabled("s", True)
        artifacts = ArtifactStore(Path(self.temp.name) / "artifacts", renderer=render_visual)
        runner = FakeRunner([plan(visual_request="画一张光合作用示意图")], visuals=[photosynthesis_spec()])
        output, _ = await LearningPipeline(
            runner, self.research, self.store, artifacts, RESPONSE_SCHEMA,
        ).run(messages("画一张光合作用示意图"), session_id="s", turn_id="1")
        validate(output, RESPONSE_SCHEMA)
        self.assertIn("打开互动图", output["message"])
        token = re.search(r"/artifacts/([A-Za-z0-9_-]+)", output["message"]).group(1)
        artifact = artifacts.get(token)
        self.assertIn("<svg", artifact)
        self.assertNotIn("<script", artifact)

    async def test_diagram_works_with_legacy_two_arg_prepare(self):
        self.store.set_enabled("s", True)
        artifacts = FakeArtifacts()
        runner = FakeRunner([plan(visual_request="画一张示意图")], visuals=[photosynthesis_spec()])
        output, _ = await LearningPipeline(
            runner, self.research, self.store, artifacts, RESPONSE_SCHEMA,
        ).run(messages("画一张示意图"), session_id="s", turn_id="1")
        validate(output, RESPONSE_SCHEMA)
        self.assertEqual(len(artifacts.committed), 1)
        self.assertEqual(artifacts.committed[0][1]["spec"]["kind"], "diagram")


if __name__ == "__main__":
    unittest.main()
