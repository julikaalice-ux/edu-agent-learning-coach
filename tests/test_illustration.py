"""Tests for the illustration visual kind and its image-generation wiring."""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src/agent-framework-agent-with-local-tools-responses"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.contracts import validate  # noqa: E402
from learning_coach.memory import LearningStore  # noqa: E402
from learning_coach.pipeline import LearningPipeline  # noqa: E402
from learning_coach.visuals import VISUAL_SCHEMA, ArtifactStore, render_visual  # noqa: E402
from test_pipeline import (  # noqa: E402
    FakeArtifacts, FakeResearch, FakeRunner, RESPONSE_SCHEMA, messages, plan,
)


# Smallest valid PNG: 1x1 transparent pixel.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


class IllustrationRenderTests(unittest.TestCase):
    def test_illustration_renders_embedded_image_page(self):
        spec = {"kind": "illustration", "title": "细胞结构示意图",
                "prompt": "a plant cell diagram", "alt": "植物细胞结构"}
        document = render_visual(spec, image=TINY_PNG)
        self.assertIn("data:image/png;base64,", document)
        self.assertIn("细胞结构示意图", document)

    def test_illustration_requires_image_bytes(self):
        spec = {"kind": "illustration", "title": "t", "prompt": "p", "alt": "a"}
        with self.assertRaises(ValueError):
            render_visual(spec)

    def test_illustration_spec_validates_against_visual_schema(self):
        validate({"kind": "illustration", "title": "t", "prompt": "p", "alt": "a"}, VISUAL_SCHEMA)
        with self.assertRaises(Exception):
            validate({"kind": "illustration", "title": "t", "alt": "a"}, VISUAL_SCHEMA)


class IllustrationPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")
        self.research = FakeResearch()
        self.artifacts = FakeArtifacts()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner):
        return LearningPipeline(runner, self.research, self.store, self.artifacts, RESPONSE_SCHEMA)

    async def test_illustration_visual_commits_generated_image(self):
        self.store.set_enabled("s", True)
        self.artifacts = ArtifactStore(Path(self.temp.name) / "artifacts")
        visual = {"kind": "illustration", "title": "圆明园复原图",
                  "prompt": "reconstructed view of Old Summer Palace, illustration",
                  "alt": "圆明园复原想象图"}
        runner = FakeRunner([plan(visual_request="配一张圆明园的画面")], visuals=[visual])
        with patch("learning_coach.illustrate.generate_image", return_value=TINY_PNG) as generate:
            output, _ = await self.pipeline(runner).run(messages("给我看圆明园的图"), session_id="s", turn_id="1")
        generate.assert_called_once_with(visual["prompt"])
        validate(output, RESPONSE_SCHEMA)
        self.assertIn("打开互动图", output["message"])
        token = re.search(r"/artifacts/([A-Za-z0-9_-]+)", output["message"]).group(1)
        self.assertIn("data:image/png;base64,", self.artifacts.get(token))

    async def test_illustration_failure_falls_back_with_notice(self):
        self.store.set_enabled("s", True)
        visual = {"kind": "illustration", "title": "t", "prompt": "p", "alt": "a"}
        runner = FakeRunner([plan(visual_request="配一张图")], visuals=[visual])
        with patch("learning_coach.illustrate.generate_image", side_effect=RuntimeError("backend_down")):
            output, _ = await self.pipeline(runner).run(messages("给我看一张图"), session_id="s", turn_id="1")
        validate(output, RESPONSE_SCHEMA)
        self.assertIn("未生成可用的互动图", output["message"])
        self.assertEqual(self.artifacts.committed, [])


if __name__ == "__main__":
    unittest.main()
