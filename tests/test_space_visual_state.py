"""Exercise Space response state without starting Gradio or a model client."""

from collections import deque
from datetime import date
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))


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


class SpaceTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pipeline = PipelineStub()
        self.created = {}
        server = ModuleType("learning_coach.server")
        server.create_pipeline = lambda **kwargs: (self.created.update(kwargs), self.pipeline)[1]
        dotenv = ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        spec = importlib.util.spec_from_file_location("space_visual_state_test", ROOT / "space/app.py")
        self.app = importlib.util.module_from_spec(spec)
        # Only external UI/client setup is stubbed; respond/new_session are real.
        with patch.dict(sys.modules, {"gradio": ModuleType("gradio"),
                                      "dotenv": dotenv, "learning_coach.server": server}):
            spec.loader.exec_module(self.app)
        self.app.gr.update = lambda **kwargs: kwargs
        self.session = self.app.new_session()[0]

    async def send(self, text, allowed=True):
        return [event async for event in self.app.respond(text, self.session, None, allowed)]


class SpaceVisualStateTests(SpaceTestBase):

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

    async def test_seen_marker_survives_but_learning_is_session_scoped(self):
        self.pipeline.responses.extend([response("你好，我是学习教练。"), response("欢迎回来。")])
        first = [event async for event in self.app.respond("你好", self.session, None, True)]
        device = first[-1][4]
        self.assertIs(device, True)
        self.assertIs(self.pipeline.options[0]["returning_visitor"], False)
        self.assertIsNone(self.pipeline.options[0]["learner_id"])
        # "新会话" starts a new conversation; only the bare seen marker survives.
        self.session = self.app.new_session()[0]
        [event async for event in self.app.respond("在吗", self.session, device, True)]
        self.assertIs(self.pipeline.options[1]["returning_visitor"], True)
        self.assertIsNone(self.pipeline.options[1]["learner_id"])
        self.assertNotEqual(self.pipeline.options[1]["session_id"], self.pipeline.options[0]["session_id"])

    async def test_reopening_starts_a_fresh_session_without_previous_context(self):
        self.pipeline.responses.extend([response("第一句回答。"), response("第二句回答。")])
        await self.send("讲讲分数")
        await self.send("继续")
        self.assertEqual(len(self.pipeline.calls[-1]), 3)  # one open page: history accumulates
        # Close and reopen: a brand-new session knows nothing of the previous one.
        self.session = self.app.new_session()[0]
        [event async for event in self.app.respond("讲讲分数", self.session, None, True)]
        self.assertEqual(self.pipeline.calls[-1], [{"role": "user", "text": "讲讲分数"}])
        self.assertIsNone(self.pipeline.options[-1]["learner_id"])
        self.assertIs(self.pipeline.options[-1]["returning_visitor"], False)

    async def test_numbered_choices_become_buttons_including_skip(self):
        question = {"schema_version": "1.0", "intent": {"primary": "G", "is_implicit": False},
                    "message": "你好！", "next_step": {"kind": "question", "text":
                    "这次是谁在学？\n1. 我自己\n2. 我是家长或老师，帮孩子用\n（回复数字或选项文字；也可以回复“跳过”）"}}
        self.pipeline.responses.extend([question, response("好的。")])
        await self.send("你好")
        self.assertEqual(self.session["options"], ["我自己", "我是家长或老师，帮孩子用", "跳过"])
        await self.send("我自己")
        self.assertEqual(self.pipeline.calls[-1][-1]["text"], "我自己")
        self.assertEqual(self.session["options"], [])

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


class SpaceAccessTests(SpaceTestBase):
    """Invite link and daily caps."""

    def request(self, **query):
        return SimpleNamespace(query_params=query)

    async def test_invite_key_is_checked_on_page_load(self):
        self.app.ACCESS_KEY = "k3y"
        self.assertEqual(self.app.check_access(self.request(key="k3y"))[0], True)
        for request in (self.request(key="wrong"), self.request(), None):
            allowed, main, note = self.app.check_access(request)
            self.assertIs(allowed, False)
            self.assertEqual((main, note), ({"visible": False}, {"visible": True}))
        self.app.ACCESS_KEY = ""
        self.assertIs(self.app.check_access(self.request())[0], True)

    async def test_turn_without_access_never_reaches_the_model(self):
        events = await self.send("你好", allowed=False)
        self.assertEqual(self.pipeline.calls, [])
        self.assertEqual(events[-1][1][-1]["content"], self.app.LOCKED_TEXT)

    async def test_daily_turn_cap(self):
        self.app.TURN_LIMIT = self.app.DailyLimit(1)
        self.pipeline.responses.append(response("好的。"))
        await self.send("第一句")
        events = await self.send("第二句")
        self.assertEqual(len(self.pipeline.calls), 1)
        self.assertEqual(events[-1][1][-1]["content"], self.app.LIMIT_TEXT)

    async def test_daily_cap_resets_on_a_new_day_and_zero_means_no_cap(self):
        limit = self.app.DailyLimit(1)
        self.assertTrue(limit.take())
        self.assertFalse(limit.take())
        limit.day = date(2000, 1, 1)
        self.assertTrue(limit.take())
        unlimited = self.app.DailyLimit(0)
        self.assertTrue(all(unlimited.take() for _ in range(500)))

    async def test_image_cap_refuses_before_calling_the_image_service(self):
        self.assertIs(self.created["image_generator"], self.app.limited_image)
        self.app.IMAGE_LIMIT = self.app.DailyLimit(1)
        self.app.IMAGE_LIMIT.take()
        with self.assertRaisesRegex(RuntimeError, "image_daily_limit"):
            await self.app.limited_image("叶片")



class SessionNotesTests(SpaceTestBase):
    """Learning notes belong to the open page session only; nothing persists across opens."""

    async def test_notes_are_shown_in_plain_words_and_only_with_access(self):
        notes = {"profile": {"learning_stage": {"value": "初中", "origin": "self_report"}},
                 "concepts": [{"concept_id": "fraction", "label": "分数的意义", "mastery_level": 0.75,
                               "attempts": 4, "correct_count": 3},
                              {"concept_id": "decimal", "label": "小数", "mastery_level": None, "attempts": 0}],
                 "error_records": [{"concept_id": "fraction", "category": "conceptual"}],
                 "review_plan": [{"topic": "分数比较", "deadline": "周五"}]}
        text = self.app.render_notes(notes)
        for expected in ("学段：初中", "分数的意义：掌握得不错（做过 4 题，对 3 题）", "小数：还没做过题",
                         "分数的意义：概念性误解", "分数比较（周五）"):
            self.assertIn(expected, text)
        self.assertEqual(self.app.render_notes({}), self.app.NO_NOTES)

        class Store:
            enabled = False

            def __init__(self):
                self.seen_keys = []

            def is_enabled(self, key):
                self.seen_keys.append(key)
                return self.enabled

            def snapshot(self, key):
                self.seen_keys.append(key)
                return notes

        self.pipeline.store = Store()
        session = self.app.new_session()[0]
        self.assertEqual(self.app.notes_markdown(session, False), self.app.LOCKED_TEXT)
        self.assertIn("还没有开启学习记录", self.app.notes_markdown(session, True))
        self.pipeline.store.enabled = True
        self.assertIn("分数的意义", self.app.notes_markdown(session, True))
        self.assertEqual(set(self.pipeline.store.seen_keys), {session["session_id"]})


if __name__ == "__main__":
    unittest.main()
