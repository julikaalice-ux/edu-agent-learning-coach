"""Exercise Space response state without starting Gradio or a model client."""

from collections import deque
from datetime import date
import asyncio
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
        spec = importlib.util.spec_from_file_location("space_visual_state_test", ROOT / "space/app.py")
        self.app = importlib.util.module_from_spec(spec)
        # Only external UI/client setup is stubbed; respond/new_session are real.
        with patch("dotenv.load_dotenv", return_value=None), patch.dict(
                sys.modules, {"gradio": ModuleType("gradio"), "learning_coach.server": server}):
            spec.loader.exec_module(self.app)
        self.app.gr.update = lambda **kwargs: kwargs
        self.app.gr.skip = lambda: {"__type__": "update"}
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
        # A skipped update leaves the live DOM (including a collapsed details
        # element and the learner's interactions inside its frame) untouched.
        self.assertTrue(all(event[2] == self.app.gr.skip() for event in second))
        self.assertEqual(self.session["panel"], panel)
        self.assertEqual(self.session["panel_token"], token)

    async def test_panel_can_be_collapsed_and_reopened_without_reloading(self):
        panel = self.app.iframe_for('<h1 title="演示">观察 & 比较</h1>')
        self.assertIn("<details", panel)
        self.assertIn(" open", panel)
        self.assertIn("<summary", panel)
        self.assertIn("</summary>", panel)
        self.assertIn("</details>", panel)
        self.assertLess(panel.index("<summary"), panel.index("<iframe"))
        self.assertIn('sandbox="allow-scripts"', panel)
        self.assertIn("&quot;演示&quot;", panel)
        self.assertIn("&amp;", panel)
        self.assertIn("&lt;h1", panel)
        self.assertIn("&lt;/h1&gt;", panel)

    async def test_same_artifact_reference_keeps_current_panel_untouched(self):
        token = "a" * 32
        self.pipeline.documents[token] = "<h1>同一幅互动图</h1>"
        output = response(f"继续观察。\n\n[打开互动图](http://localhost:8088/artifacts/{token})")
        self.pipeline.responses.extend([output, output])
        first = await self.send("画图")
        panel = first[-1][2]
        second = await self.send("再解释一下这张图")
        self.assertTrue(all(event[2] == self.app.gr.skip() for event in second))
        self.assertEqual(self.session["panel"], panel)
        self.assertEqual(self.session["panel_token"], token)

    async def test_new_artifact_replaces_panel_only_when_ready(self):
        for token, title in (("a" * 32, "斜率"), ("b" * 32, "截距")):
            self.pipeline.documents[token] = f"<h1>{title}</h1>"
            self.pipeline.responses.append(response(
                f"观察{title}。\n\n[打开互动图](http://localhost:8088/artifacts/{token})"))
        first = await self.send("先看斜率")
        second = await self.send("换一张图看截距")
        self.assertEqual(second[0][2], self.app.gr.skip())
        self.assertIn("截距", second[-1][2])
        self.assertNotEqual(first[-1][2], second[-1][2])
        self.assertEqual(self.session["panel"], second[-1][2])
        self.assertEqual(self.session["panel_token"], "b" * 32)

    async def test_new_token_reopens_panel_even_when_document_is_identical(self):
        document = "<h1>相同内容的新演示</h1>"
        tokens = ("a" * 32, "b" * 32)
        for token in tokens:
            self.pipeline.documents[token] = document
            self.pipeline.responses.append(response(
                f"观察演示。\n\n[打开互动图](http://localhost:8088/artifacts/{token})"))
        first = await self.send("生成一张图")
        second = await self.send("重新生成一张图")
        first_panel, second_panel = first[-1][2], second[-1][2]
        # Gradio deduplicates equal HTML values; artifact identity must make a
        # fresh result replace the old DOM even when the renderer repeats it.
        self.assertNotEqual(first_panel, second_panel)
        self.assertIn(f'data-artifact="{tokens[0]}"', first_panel)
        self.assertIn(f'data-artifact="{tokens[1]}"', second_panel)
        self.assertEqual(second[0][2], self.app.gr.skip())
        self.assertEqual(self.session["panel"], second_panel)
        self.assertEqual(self.session["panel_token"], tokens[1])

    async def test_missing_artifact_and_failure_preserve_existing_panel(self):
        self.session.update(panel="existing interactive frame", panel_token="a" * 32)
        self.pipeline.responses.append(response(
            "暂时打开不了。\n\n[打开互动图](http://localhost:8088/artifacts/missing)"))
        missing = await self.send("看图")
        # An empty stub response queue then raises inside the pipeline.
        failed = await self.send("继续")
        self.assertIn("没有顺利完成", failed[-1][1][-1]["content"])
        for events in (missing, failed):
            self.assertTrue(all(event[2] == self.app.gr.skip() for event in events))
        self.assertEqual(self.session["panel"], "existing interactive frame")
        self.assertEqual(self.session["panel_token"], "a" * 32)
        self.assertFalse(self.session["locked"])

    async def test_empty_locked_and_refused_turns_do_not_reset_panel(self):
        self.session.update(panel="existing interactive frame", panel_token="a" * 32)
        empty = await self.send("  ")
        self.session["locked"] = True
        locked = await self.send("继续")
        self.session["locked"] = False
        refused = await self.send("继续", allowed=False)
        for events in (empty, locked, refused):
            self.assertTrue(all(event[2] == self.app.gr.skip() for event in events))
        self.assertEqual(self.pipeline.calls, [])
        self.assertEqual(self.session["panel"], "existing interactive frame")

    async def test_new_session_clears_the_panel_and_its_artifact_identity(self):
        self.session.update(panel="existing interactive frame", panel_token="a" * 32)
        session, chat, panel, text = self.app.new_session()
        self.assertEqual((chat, panel, text), ([], "", ""))
        self.assertEqual(session["panel"], "")
        self.assertIsNone(session["panel_token"])
        self.assertNotEqual(session["session_id"], self.session["session_id"])

    async def test_cancelled_nonstream_turn_cancels_and_joins_background_work(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        class PendingPipeline(PipelineStub):
            async def run(self, messages, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    # Make cleanup asynchronous: respond must await the child,
                    # not just call cancel() and abandon its resources.
                    await asyncio.sleep(0)
                    finished.set()

        self.app.PIPELINE = PendingPipeline()
        generator = self.app.respond("生成互动图", self.session, None, True)
        acknowledgement = await anext(generator)
        self.assertEqual(acknowledgement[2], self.app.gr.skip())
        pending = asyncio.create_task(anext(generator))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(finished.is_set())
            self.assertFalse(self.session["locked"])
        finally:
            pending.cancel()
            await generator.aclose()

    async def test_progress_preserves_panel_and_closing_generator_stops_work(self):
        finished = asyncio.Event()
        app = self.app

        class ProgressPipeline(PipelineStub):
            async def run(self, messages, **kwargs):
                app.STAGE_STATUS.current = "正在生成互动图…"
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    finished.set()

        self.app.PIPELINE = ProgressPipeline()
        self.session.update(panel="existing interactive frame", panel_token="a" * 32)
        generator = self.app.respond("换一个演示", self.session, None, True)
        try:
            await anext(generator)  # Immediate acknowledgement.
            progress = await asyncio.wait_for(anext(generator), timeout=2)
            self.assertEqual(progress[1][-1]["content"], "正在生成互动图…")
            self.assertEqual(progress[2], self.app.gr.skip())
            self.assertEqual(self.session["panel"], "existing interactive frame")
        finally:
            # Gradio closes a suspended generator when cancelling its event.
            await generator.aclose()
        self.assertTrue(finished.is_set())
        self.assertFalse(self.session["locked"])

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

    async def test_streaming_reply_appears_incrementally_then_finalizes(self):
        final_output = response("分数表示整体被平均分成的份数。")

        class StreamingPipeline(PipelineStub):
            async def run_stream(self, messages, **kwargs):
                self.calls.append(messages)
                self.options.append(kwargs)
                for piece in ("分数表示", "整体被平均", "分成的份数。"):
                    yield {"delta": piece}
                yield {"output": final_output, "state": {}}

        self.app.PIPELINE = StreamingPipeline()
        self.session.update(panel="existing interactive frame", panel_token="a" * 32)
        events = [event async for event in self.app.respond("分数是什么", self.session, None, True)]
        self.assertEqual(events[0][1][-1]["content"], "我先想想…")
        for event, piece in zip(events[1:4], ("分数表示", "分数表示整体被平均", "分数表示整体被平均分成的份数。")):
            self.assertEqual(event[1][-1]["content"], piece)
        self.assertEqual(events[-1][1][-1]["content"], "分数表示整体被平均分成的份数。")
        self.assertEqual(self.session["history"][-1]["text"], "分数表示整体被平均分成的份数。")
        self.assertIs(events[-1][4], True)  # seen marker after the first turn
        self.assertTrue(all(event[2] == self.app.gr.skip() for event in events))
        self.assertEqual(self.session["panel"], "existing interactive frame")
        self.assertEqual(self.session["panel_token"], "a" * 32)

    async def test_early_explanation_stays_in_one_bubble_and_commits_only_at_final(self):
        explanation = "光遇到微小颗粒时会向不同方向散射。"
        token = "c" * 32
        final_output = response(
            f"{explanation}\n\n[打开互动图](http://localhost:8088/artifacts/{token})")
        final_output["next_step"] = {
            "kind": "question", "text": "接下来观察什么？\n1. 增加颗粒\n2. 减少颗粒"}
        final_state = {"ready": True}

        class ExplanationPipeline(PipelineStub):
            async def run_stream(self, messages, **kwargs):
                self.calls.append(messages)
                yield {"explanation": explanation}
                yield {"output": final_output, "state": final_state}

        pipeline = ExplanationPipeline()
        pipeline.documents[token] = "<h1>观察散射光</h1>"
        self.app.PIPELINE = pipeline
        old_state = {"ready": False}
        self.session.update(state=old_state, options=["上一题选项"],
                            panel="existing interactive frame", panel_token="a" * 32)
        generator = self.app.respond("演示丁达尔效应", self.session, None, True)
        try:
            acknowledgement = await anext(generator)
            self.assertEqual(acknowledgement[1][-1]["content"], "我先想想…")
            early = await anext(generator)
            self.assertEqual(early[1][-1]["content"], explanation)
            self.assertEqual(len(early[1]), 2)
            self.assertEqual(early[2], self.app.gr.skip())
            self.assertEqual(self.session["history"], [])
            self.assertEqual(self.session["chat"], [])
            self.assertIs(self.session["state"], old_state)
            self.assertEqual(self.session["options"], ["上一题选项"])
            self.assertEqual(self.session["panel"], "existing interactive frame")
            self.assertEqual(pipeline.calls, [[{"role": "user", "text": "演示丁达尔效应"}]])

            final = await anext(generator)
            self.assertEqual(len(final[1]), 2)
            self.assertEqual(final[1][-1]["content"].count(explanation), 1)
            self.assertIn("接下来观察什么", final[1][-1]["content"])
            self.assertIn(f'data-artifact="{token}"', final[2])
            self.assertIs(self.session["state"], final_state)
            self.assertEqual(self.session["options"], ["增加颗粒", "减少颗粒"])
            self.assertEqual(len(self.session["history"]), 2)
            self.assertEqual(self.session["history"][0], {"role": "user", "text": "演示丁达尔效应"})
            self.assertEqual(self.session["history"][1]["text"].count(explanation), 1)
            self.assertNotIn("/artifacts/", self.session["history"][1]["text"])
            with self.assertRaises(StopAsyncIteration):
                await anext(generator)
        finally:
            await generator.aclose()
        self.assertFalse(self.session["locked"])

    async def test_failure_after_early_explanation_keeps_the_validated_text(self):
        explanation = "分母表示整体平均分成的份数。"

        class BrokenExplanationPipeline(PipelineStub):
            async def run_stream(self, messages, **kwargs):
                yield {"explanation": explanation}
                raise RuntimeError("visual interrupted")

        self.app.PIPELINE = BrokenExplanationPipeline()
        self.session.update(panel="existing interactive frame", panel_token="a" * 32,
                            state={"previous": True}, options=["上一题选项"])
        events = await self.send("演示一下分数")
        self.assertEqual(len(events), 3)  # acknowledgement, explanation, failure
        last_text = events[-1][1][-1]["content"]
        self.assertTrue(last_text.startswith(explanation))
        self.assertEqual(last_text.count(explanation), 1)
        self.assertIn("后续内容暂时没有完成", last_text)
        self.assertEqual(len(events[-1][1]), 2)
        self.assertTrue(all(event[2] == self.app.gr.skip() for event in events))
        self.assertEqual(self.session["panel"], "existing interactive frame")
        self.assertEqual(self.session["history"], [])
        self.assertEqual(self.session["state"], {"previous": True})
        self.assertEqual(self.session["options"], ["上一题选项"])
        self.assertFalse(self.session["locked"])

    async def test_closing_after_explanation_joins_worker_and_never_publishes_late_panel(self):
        started = asyncio.Event()
        finished = asyncio.Event()
        late_replies = []
        token = "c" * 32

        async def build_visual():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                finished.set()

        class WorkingExplanationPipeline(PipelineStub):
            async def run_stream(self, messages, **kwargs):
                worker = asyncio.create_task(build_visual())
                try:
                    await started.wait()
                    yield {"explanation": "光会被颗粒散射。"}
                    await worker
                    late_replies.append(True)
                    yield {"output": response(
                        f"看演示。\n\n[打开互动图](http://localhost:8088/artifacts/{token})"),
                        "state": {"ready": True}}
                finally:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass

        pipeline = WorkingExplanationPipeline()
        pipeline.documents[token] = "<h1>不应发布的迟到图片</h1>"
        self.app.PIPELINE = pipeline
        self.session.update(panel="existing interactive frame", panel_token="a" * 32)
        generator = self.app.respond("演示散射", self.session, None, True)
        try:
            await anext(generator)
            early = await asyncio.wait_for(anext(generator), timeout=1)
            self.assertEqual(early[1][-1]["content"], "光会被颗粒散射。")
            self.assertFalse(finished.is_set())
        finally:
            await generator.aclose()
        self.assertTrue(finished.is_set())
        self.assertEqual(late_replies, [])
        self.assertEqual(self.session["panel"], "existing interactive frame")
        self.assertEqual(self.session["panel_token"], "a" * 32)
        self.assertEqual(self.session["history"], [])
        self.assertEqual(self.session["chat"], [])
        self.assertIsNone(self.session["state"])
        self.assertFalse(self.session["locked"])

    async def test_final_reply_is_committed_once_and_closes_the_stream(self):
        continued = []

        class DuplicateFinalPipeline(PipelineStub):
            async def run_stream(self, messages, **kwargs):
                yield {"explanation": "先看解释。"}
                yield {"output": response("最终回答。"), "state": {}}
                continued.append(True)
                yield {"output": response("不应提交的重复回答。"), "state": {}}

        self.app.PIPELINE = DuplicateFinalPipeline()
        events = await self.send("解释一下")
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1][1][-1]["content"], "最终回答。")
        self.assertEqual(self.session["chat"], events[-1][1])
        self.assertEqual(self.session["history"], [
            {"role": "user", "text": "解释一下"},
            {"role": "assistant", "text": "最终回答。"},
        ])
        self.assertEqual(continued, [])

    async def test_streaming_failure_does_not_reopen_the_panel(self):
        class BrokenStreamingPipeline(PipelineStub):
            async def run_stream(self, messages, **kwargs):
                yield {"delta": "先看"}
                raise RuntimeError("stream interrupted")

        self.app.PIPELINE = BrokenStreamingPipeline()
        self.session.update(panel="existing interactive frame", panel_token="a" * 32)
        events = await self.send("继续解释")
        self.assertTrue(all(event[2] == self.app.gr.skip() for event in events))
        self.assertIn("没有顺利完成", events[-1][1][-1]["content"])
        self.assertEqual(self.session["panel"], "existing interactive frame")
        self.assertFalse(self.session["locked"])

    async def test_closing_after_stream_delta_waits_for_inner_stream_cleanup(self):
        finished = asyncio.Event()
        resumed = []

        class SuspendedStreamingPipeline(PipelineStub):
            async def run_stream(self, messages, **kwargs):
                try:
                    yield {"delta": "先观察这张图"}
                    resumed.append(True)
                    yield {"output": response("完整回答。"), "state": {}}
                finally:
                    # The enclosing generator must await this cleanup before
                    # reset finishes, even while suspended at a yielded delta.
                    await asyncio.sleep(0)
                    finished.set()

        self.app.PIPELINE = SuspendedStreamingPipeline()
        generator = self.app.respond("解释这张图", self.session, None, True)
        try:
            await anext(generator)  # Immediate acknowledgement.
            delta = await anext(generator)
            self.assertEqual(delta[1][-1]["content"], "先观察这张图")
            self.assertFalse(finished.is_set())
            self.assertTrue(self.session["locked"])
        finally:
            await generator.aclose()
        self.assertTrue(finished.is_set())
        self.assertEqual(resumed, [])
        self.assertFalse(self.session["locked"])
        self.assertFalse(any(item["role"] == "assistant" for item in self.session["history"]))

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


class SpaceArtifactTests(SpaceTestBase):
    def test_question_picture_is_taken_out_of_its_page(self):
        page = '<html><img alt="x" src="data:image/png;base64,iVBORw0KGgo="></html>'
        self.assertEqual(self.app.page_picture(page), ("image/png", b"\x89PNG\r\n\x1a\n"))
        self.assertIsNone(self.app.page_picture("<html>no picture</html>"))
        self.assertIsNone(self.app.page_picture(None))

    def test_hosted_visuals_are_fetched_from_public_storage_only_by_token(self):
        proxy_spec = importlib.util.spec_from_file_location("proxy_artifact_test", ROOT / "backend/proxy.py")
        proxy = importlib.util.module_from_spec(proxy_spec)
        proxy_spec.loader.exec_module(proxy)
        seen = []

        class Page:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return "<html>互动图</html>".encode()

        def opener(url, timeout):
            seen.append(url)
            return Page()
        with patch.object(proxy.urllib.request, "urlopen", opener):
            self.assertEqual(proxy.fetch_artifact("t" * 32), "<html>互动图</html>")
            # Anything that is not a plain token never reaches the network.
            self.assertIsNone(proxy.fetch_artifact("../secret"))
            self.assertIsNone(proxy.fetch_artifact("t" * 32 + "/../x"))
        self.assertEqual(seen, [proxy.ARTIFACT_BASE + "/" + "t" * 32 + ".html"])


class SpaceTurnTimingTests(SpaceTestBase):
    def test_turn_timing_logs_step_times_only(self):
        timer = self.app.TurnTimer()
        timer.mark("token_ms")
        timer.mark("token_ms")  # the first mark of a step counts
        timer.mark("first_text_ms")
        with self.assertLogs("learning_coach", level="INFO") as logs:
            timer.log("ok")
        line = logs.records[-1].getMessage()
        self.assertRegex(line, r"^turn_timing outcome=ok token_ms=\d+ first_text_ms=\d+ total_ms=\d+$")


if __name__ == "__main__":
    unittest.main()
