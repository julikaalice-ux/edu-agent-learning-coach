"""Exercise Space response state without starting Gradio or a model client."""

from collections import deque
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/agent-framework-agent-with-local-tools-responses"))


class PipelineStub:
    def __init__(self):
        self.responses = deque()
        self.calls = []
        self.options = []
        self.documents = {}
        self.artifacts = self

    def get(self, token):
        return self.documents.get(token)

    async def run(self, messages, **kwargs):
        self.calls.append(messages)
        self.options.append(kwargs)
        return self.responses.popleft(), {}


def response(message):
    return {"schema_version": "1.0", "intent": {"primary": "A", "is_implicit": False},
            "message": message, "next_step": None}


class SpaceVisualStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pipeline = PipelineStub()
        server = ModuleType("learning_coach.server")
        server.create_pipeline = lambda **kwargs: self.pipeline
        dotenv = ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        spec = importlib.util.spec_from_file_location("space_visual_state_test", ROOT / "space/app.py")
        self.app = importlib.util.module_from_spec(spec)
        # Only external UI/client setup is stubbed; respond/new_session are real.
        with patch.dict(sys.modules, {"gradio": ModuleType("gradio"),
                                      "dotenv": dotenv, "learning_coach.server": server}):
            spec.loader.exec_module(self.app)
        self.session = self.app.new_session()[0]

    async def send(self, text):
        return [event async for event in self.app.respond(text, self.session)]

    async def test_interactive_panel_survives_next_plain_turn(self):
        token = "a" * 32
        self.pipeline.documents[token] = "<!doctype html><h1>观察斜率</h1>"
        self.pipeline.responses.extend([
            response(f"拖动斜率滑块。\n\n[打开互动图](http://localhost:8088/artifacts/{token})"),
            response("现在看看截距。"),
        ])
        first = await self.send("画出一次函数")
        panel = first[-1][2]
        self.assertIn("<iframe", panel)
        self.assertEqual(self.session["panel"], panel)
        second = await self.send("接着讲")
        self.assertEqual([event[2] for event in second], [panel, panel])
        self.assertEqual(self.session["panel"], panel)

    async def test_device_marker_survives_new_conversation(self):
        self.pipeline.responses.extend([response("你好，我是学习教练。"), response("欢迎回来。")])
        first = [event async for event in self.app.respond("你好", self.session, None)]
        device = first[-1][4]
        self.assertTrue(device["learner_id"])
        self.assertIs(device["seen"], True)
        self.assertIs(self.pipeline.options[0]["returning_visitor"], False)
        # "新会话" starts a new conversation; the browser keeps the device marker.
        self.session = self.app.new_session()[0]
        [event async for event in self.app.respond("在吗", self.session, device)]
        self.assertIs(self.pipeline.options[1]["returning_visitor"], True)
        self.assertEqual(self.pipeline.options[1]["learner_id"], device["learner_id"])
        self.assertNotEqual(self.pipeline.options[1]["session_id"], self.pipeline.options[0]["session_id"])

    async def test_image_is_visible_but_never_forwarded_as_model_text(self):
        token = "b" * 32
        data_url = "data:image/png;base64,aGVsbG8="
        self.pipeline.documents[token] = f'<img src="{data_url}" alt="叶片">'
        self.pipeline.responses.extend([
            response(f"观察叶片。\n\n[打开互动图](http://localhost:8088/artifacts/{token})"),
            response("叶片的绿色来自叶绿素。"),
        ])
        first = await self.send("看看叶片")
        self.assertIn(data_url, first[-1][1][-1]["content"])
        self.assertEqual(self.session["history"][-1]["text"], "观察叶片。")
        await self.send("继续")
        self.assertFalse(any("base64" in message["text"] for message in self.pipeline.calls[-1]))
        self.assertEqual(self.pipeline.calls[-1][-2]["text"], "观察叶片。")
        self.assertFalse(self.session["locked"])


if __name__ == "__main__":
    unittest.main()
