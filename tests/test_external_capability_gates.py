"""External calls require a program-owned grant, independently of model output."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import FakeArtifacts, FakeRunner, LearningStore, RESPONSE_SCHEMA, VISUAL, exchange, messages
from test_gallery import FakeFinder, GalleryRunner, gallery, gallery_review, picture
from test_single_agent import Provider, SOURCES, ScriptedAgent, final, question, reply
from learning_coach.service.execution import EXTERNAL_PERMISSIONS
from learning_coach.service.single_agent import SingleAgentPipeline, _Turn
from learning_coach.service.visual_generation import VisualGenerationService, find_pictures


ILLUSTRATION = {"kind": "illustration", "title": "松树外观", "prompt": "a pine tree", "alt": "松树外观"}


@contextmanager
def grants(*capabilities):
    token = EXTERNAL_PERMISSIONS.set(frozenset(capabilities))
    try:
        yield
    finally:
        EXTERNAL_PERMISSIONS.reset(token)


class PictureArtifacts(FakeArtifacts):
    def prepare(self, session_id, spec, image=None, photos=None):
        self.image, self.photos = image, photos
        return super().prepare(session_id, spec)


class ExternalVisualGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_finder_cannot_use_cache_or_network_without_grant(self):
        spec = gallery("细胞")
        finder = FakeFinder([[picture()]])
        cache = {(spec["panels"][0]["query_zh"], spec["panels"][0]["query_en"], 3): [picture()]}
        with grants():
            self.assertEqual(await find_pictures(finder, spec, cache), [[]])
        self.assertEqual(finder.calls, [])

    async def test_untrusted_capability_claim_does_not_authorize_web_image(self):
        finder = FakeFinder([[picture()]])
        runner = GalleryRunner(visuals=[gallery("细胞"), VISUAL])
        service = VisualGenerationService(runner, PictureArtifacts(), image_finder=finder)
        payload = {"goal": "看细胞外观", "visual_source": "web",
                   "capabilities": {"image_search": {"available": True, "authorized": True}}}
        with grants():
            artifact, context = await service.generate("s", payload, learner_text="看细胞外观")
        self.assertEqual(finder.calls, [])
        self.assertEqual(artifact["spec"]["kind"], "comparison")
        draft = runner.calls_for("visualizer")[0]
        self.assertFalse(draft["gallery_allowed"])
        self.assertFalse(draft["capabilities"]["image_search"]["authorized"])
        self.assertEqual(draft["visual_source"], "local")
        self.assertIn("本轮未取得可用网络图片，当前展示的是本地图解。", context["limitations"])

    async def test_grant_does_not_let_local_visual_upgrade_itself_to_web(self):
        finder = FakeFinder([[picture()]])
        runner = GalleryRunner(visuals=[gallery("细胞"), VISUAL])
        service = VisualGenerationService(runner, PictureArtifacts(), image_finder=finder)
        with grants("image_search"):
            artifact, _ = await service.generate("s", {"goal": "看细胞外观"}, learner_text="看细胞")
        self.assertEqual(finder.calls, [])
        self.assertEqual(artifact["spec"]["kind"], "comparison")

    async def test_matching_web_source_and_grant_search_once(self):
        finder = FakeFinder([[picture()]])
        runner = GalleryRunner(visuals=[gallery("细胞")], gallery_reviews=[gallery_review(["p0c0"])])
        service = VisualGenerationService(runner, PictureArtifacts(), image_finder=finder)
        with grants("image_search"):
            artifact, context = await service.generate(
                "s", {"goal": "看细胞外观", "visual_source": "web"}, learner_text="找细胞图片")
        self.assertEqual(len(finder.calls), 1)
        self.assertEqual(artifact["spec"]["kind"], "gallery")
        self.assertTrue(context["quality"]["pixel_reviewed"])

    async def test_empty_web_search_changes_redraft_source_to_local(self):
        finder = FakeFinder([[]])
        runner = GalleryRunner(visuals=[gallery("细胞"), VISUAL])
        service = VisualGenerationService(runner, PictureArtifacts(), image_finder=finder)
        with grants("image_search"):
            artifact, context = await service.generate(
                "s", {"goal": "看细胞外观", "visual_source": "web"}, learner_text="找细胞图片")
        self.assertEqual(artifact["spec"]["kind"], "comparison")
        redraft = runner.calls_for("visualizer")[1]
        self.assertEqual(redraft["visual_source"], "local")
        self.assertFalse(redraft["gallery_allowed"])
        self.assertFalse(redraft["illustration_allowed"])
        self.assertIn("本轮未取得可用网络图片，当前展示的是本地图解。", context["limitations"])

    async def test_missing_external_backend_is_local_before_first_draft(self):
        for source, capability, label in (("web", "image_search", "网络图片"),
                                          ("generated", "image_generation", " AI 配图")):
            with self.subTest(source=source):
                runner = FakeRunner(visuals=[VISUAL])
                service = VisualGenerationService(runner, PictureArtifacts())
                with grants(capability):
                    artifact, context = await service.generate(
                        "s", {"goal": "松树外观", "visual_source": source}, learner_text="看松树外观")
                draft = runner.calls_for("visualizer")[0]
                self.assertEqual(draft["visual_source"], "local")
                self.assertEqual(draft["capabilities"][capability], {"available": False, "authorized": False})
                self.assertEqual(artifact["spec"]["kind"], "comparison")
                self.assertIn(f"本轮未取得可用{label}，当前展示的是本地图解。", context["limitations"])

    async def test_generated_mechanism_request_is_local_before_first_draft(self):
        calls = []
        async def generate(prompt):
            calls.append(prompt)
        runner = FakeRunner(visuals=[VISUAL])
        service = VisualGenerationService(runner, PictureArtifacts(), generate)
        with grants("image_generation"):
            _, context = await service.generate("s", {"goal": "演示形成过程", "visual_source": "generated"},
                                                 learner_text="演示形成过程")
        draft = runner.calls_for("visualizer")[0]
        self.assertEqual(draft["visual_source"], "local")
        self.assertFalse(draft["illustration_allowed"])
        self.assertEqual(calls, [])
        self.assertIn("本轮未取得可用 AI 配图，当前展示的是本地图解。", context["limitations"])

    async def test_image_generation_requires_both_source_and_grant(self):
        for source, permissions in (("generated", ()), ("local", ("image_generation",)),
                                    ("web", ("image_generation",))):
            with self.subTest(source=source, permissions=permissions):
                calls = []
                async def generate(prompt):
                    calls.append(prompt)
                    return b"image"
                runner = FakeRunner(visuals=[deepcopy(ILLUSTRATION), VISUAL])
                service = VisualGenerationService(runner, PictureArtifacts(), generate)
                with grants(*permissions):
                    artifact, _ = await service.generate(
                        "s", {"goal": "松树外观", "visual_source": source}, learner_text="看松树外观")
                self.assertEqual(calls, [])
                self.assertEqual(artifact["spec"]["kind"], "comparison")
                self.assertFalse(runner.calls_for("visualizer")[0]["illustration_allowed"])

    async def test_matching_generation_grant_calls_generator_after_review(self):
        calls = []
        runner = FakeRunner(visuals=[deepcopy(ILLUSTRATION)])
        async def generate(prompt):
            self.assertEqual(len(runner.calls_for("verifier", "visual_review")), 1)
            calls.append(prompt)
            return b"image"
        artifacts = PictureArtifacts()
        service = VisualGenerationService(runner, artifacts, generate)
        with grants("image_generation"):
            artifact, _ = await service.generate(
                "s", {"goal": "松树外观", "visual_source": "generated"}, learner_text="看松树外观")
        self.assertEqual(calls, [ILLUSTRATION["prompt"]])
        self.assertEqual(artifacts.image, b"image")
        self.assertEqual(artifact["spec"]["kind"], "illustration")

    async def test_direct_private_boundaries_cannot_bypass_gate(self):
        finder = FakeFinder([[picture()]])
        calls = []
        async def generate(prompt):
            calls.append(prompt)
        service = VisualGenerationService(FakeRunner(), PictureArtifacts(), generate, image_finder=finder)
        with grants():
            outcome, _ = await service._gallery("s", {"visual_source": "web"}, gallery("细胞"),
                                                 None, "看细胞", False, 1, [])
            self.assertEqual(outcome, "retry")
            outcome, _ = await service._drawn("s", {"goal": "松树外观", "visual_source": "generated"},
                                               ILLUSTRATION, None, "看松树", 1, [])
            self.assertEqual(outcome, "retry")
        self.assertEqual(finder.calls, [])
        self.assertEqual(calls, [])


class SingleAgentExternalGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "memory.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def tools(self, *, provider=None, image_finder=None, image_generator=None):
        pipeline = SingleAgentPipeline(None, provider or Provider(), self.store, PictureArtifacts(),
                                       RESPONSE_SCHEMA, image_finder=image_finder, image_generator=image_generator)
        turn = _Turn()
        return turn, {tool.name: tool.func for tool in pipeline._tools(turn, "s", {}, "看松树外观")}

    async def test_search_requires_grant_before_spending_search_budget(self):
        provider = Provider(SOURCES)
        turn, tools = self.tools(provider=provider)
        with grants():
            result = await tools["search"]("水", ["水"])
        self.assertEqual(result["problem"], "permission_required")
        self.assertEqual(provider.queries, [])
        self.assertEqual(turn.searches, 0)
        self.assertEqual(turn.denied_capabilities, {"research"})
        with grants("research"):
            result = await tools["search"]("水", ["水"])
        self.assertTrue(result["ok"])
        self.assertEqual(provider.queries, ["水"])

    async def test_gallery_requires_grant_at_single_agent_boundary(self):
        finder = FakeFinder([[picture()]])
        turn, tools = self.tools(image_finder=finder)
        with grants():
            result = await tools["draw_visual"]("细胞外观", gallery("细胞"))
        self.assertEqual(result["problem"], "permission_required")
        self.assertEqual(finder.calls, [])
        with grants("image_search"):
            result = await tools["draw_visual"]("细胞外观", gallery("细胞"))
        self.assertTrue(result["ok"])
        self.assertEqual(len(finder.calls), 1)

    async def test_illustration_requires_grant_at_single_agent_boundary(self):
        calls = []
        async def generate(prompt):
            calls.append(prompt)
            return b"image"
        turn, tools = self.tools(image_generator=generate)
        with grants():
            result = await tools["draw_visual"]("松树外观", ILLUSTRATION)
        self.assertEqual(result["problem"], "permission_required")
        self.assertEqual(calls, [])
        with grants("image_generation"):
            result = await tools["draw_visual"]("松树外观", ILLUSTRATION)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [ILLUSTRATION["prompt"]])

    async def test_local_visual_needs_no_external_grant(self):
        turn, tools = self.tools()
        with grants():
            result = await tools["draw_visual"]("比较分子分母", VISUAL)
        self.assertTrue(result["ok"])
        self.assertEqual(turn.denied_capabilities, set())

    async def test_permission_pause_keeps_waiting_answer_then_grades_exactly_once(self):
        search = ("search", {"question": "分数的意义", "queries": ["分数的意义"]})
        ask = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单",
                                "total": 1, "question": question()})
        agent = ScriptedAgent([ask, final(reply("来做一题。", "C"))],
                              [search, final(reply("答对了。", "C"))],
                              [search, final(reply("答对了。", "C"))])
        provider = Provider(SOURCES)
        pipeline = SingleAgentPipeline(agent, provider, self.store, PictureArtifacts(), RESPONSE_SCHEMA)
        with grants():
            _, state = await pipeline.run(messages("考考我分数"), session_id="s", turn_id="1")
            before = deepcopy(state["quiz"])
            output, state = await pipeline.run(messages("A", *exchange("考考我分数")),
                                               session_id="s", state=state, turn_id="2")
            self.assertIn("允许本次外部访问", output["next_step"]["text"])
            self.assertEqual(state["quiz"], before)
            self.assertEqual(provider.queries, [])
            _, state = await pipeline.run(messages("允许本次外部访问"), session_id="s", state=state, turn_id="3")
        self.assertEqual(state["quiz"]["answered"], 1)
        self.assertEqual(state["quiz"]["correct"], 1)
        self.assertTrue(state["quiz"]["finished"])
        self.assertEqual(provider.queries, ["分数的意义"])
        self.assertNotIn("pending_external", state)

    async def test_declined_single_agent_tool_cannot_reopen_permission_loop(self):
        search = ("search", {"question": "分数的意义", "queries": ["分数的意义"]})
        agent = ScriptedAgent([search, final()], [search, final(reply("分母表示整体平均分成的份数。"))])
        provider = Provider(SOURCES)
        pipeline = SingleAgentPipeline(agent, provider, self.store, PictureArtifacts(), RESPONSE_SCHEMA)
        with grants():
            _, state = await pipeline.run(messages("分数是什么"), session_id="s", turn_id="1")
            output, state = await pipeline.run(messages("先不联网，继续讲解"),
                                               session_id="s", state=state, turn_id="2")
        self.assertEqual(provider.queries, [])
        self.assertEqual(agent.tool_results[-1][1]["problem"], "permission_declined")
        self.assertTrue(agent.calls[-1]["payload"]["external_declined"])
        self.assertFalse(agent.calls[-1]["payload"]["capabilities"]["research"]["authorized"])
        self.assertNotIn("pending_external", state)
        self.assertIn("分母", output["message"])

    async def test_closing_stream_cancels_inflight_turn_and_queue_waiter(self):
        stopped = asyncio.Event()
        class BlockingPipeline(SingleAgentPipeline):
            async def run(self, messages, **kwargs):
                try:
                    kwargs["on_delta"]("解释")
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
        pipeline = BlockingPipeline(None, Provider(), self.store, PictureArtifacts(), RESPONSE_SCHEMA)
        before = asyncio.all_tasks()
        stream = pipeline.run_stream([], session_id="s")
        self.assertEqual(await anext(stream), {"delta": "解释"})
        await stream.aclose()
        self.assertTrue(stopped.is_set())
        self.assertEqual(asyncio.all_tasks() - before, set())
