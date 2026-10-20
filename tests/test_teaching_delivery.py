"""Offline delivery behavior: complete explanations, visual handoff and cancellation.

These assertions verify routing/state boundaries, not teaching quality. Human or
model evaluation of explanation quality is specified in teaching-quality-cases.md.
"""

import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.db.memory import LearningStore
from learning_coach.domain.contracts import validate
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.execution import StageFailure
from test_pipeline import FakeArtifacts, FakeResearch, FakeRunner, QUIZ, RESPONSE_SCHEMA, messages, plan


USER = "请讲清分子与分母之间的关系。"
INITIAL = "分母表示整体平均分成的总份数，分子表示其中取出的份数。"
SUPPLEMENT = "图中的两列分别对照取出的份数和总份数。"
OBSERVATION = {"kind": "task", "text": "对照两列，指出分母对应哪一种数量。"}


def answer(message=SUPPLEMENT, step=OBSERVATION):
    return {"schema_version": "1.0", "intent": {"primary": "A", "is_implicit": False},
            "message": message, "next_step": deepcopy(step)}


def visual_decision(*, initial=INITIAL, research=False, check=None):
    selected = plan(visual_request="用结构图对照分子表示的数量与分母表示的数量。", visual_source="local")
    selected["tool_reasons"] = {
        "research": None, "quiz": None, "notes": None,
        "visual": {"code": "structure", "evidence_quote": USER},
    }
    if research:
        selected.update(research_query="分子与分母的定义", search_queries=["分子 分母 定义"],
                        visual_uses_research=True)
        selected["tool_reasons"]["research"] = {"code": "fact_check", "evidence_quote": USER}
    return {"mode": "tools", "plan": selected, "initial_explanation": initial,
            "context_notes": None, "check_question": check}


class BlockedVisualRunner(FakeRunner):
    """A visual worker that can also delay cancellation cleanup."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.exited = asyncio.Event()
        self.delay_cleanup = False

    async def __call__(self, role, payload, schema):
        result = await super().__call__(role, payload, schema)
        if role == "visualizer":
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if self.delay_cleanup:
                    await self.cleanup_release.wait()
                raise
            finally:
                self.exited.set()
        return result


class BlockedResearch(FakeResearch):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, query, queries=None):
        result = await super().run(query, queries)
        self.started.set()
        await self.release.wait()
        return result


class TeachingDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")
        self.artifacts = FakeArtifacts()
        self.research = FakeResearch()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, research=None):
        # Turns here run as returning visitors: a first question would get the
        # program's introduction in front, which these delivery tests do not test.
        return LearningPipeline(runner, research or self.research, self.store,
                                self.artifacts, RESPONSE_SCHEMA)

    async def test_ordinary_answer_stays_one_call_without_explanation_events(self):
        runner = FakeRunner([plan()], replies=[answer(INITIAL, None)])
        pipeline = self.pipeline(runner)
        events = [event async for event in pipeline.run_stream(messages(USER), session_id="plain", returning_visitor=True)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["output"], answer(INITIAL, None))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.artifacts.prepared, [])

    async def test_explanation_arrives_before_visual_and_final_contains_it_once(self):
        runner = BlockedVisualRunner([visual_decision()], replies=[answer()])
        pipeline = self.pipeline(runner)
        stream = pipeline.run_stream(messages(USER), session_id="early", returning_visitor=True)
        try:
            first = await asyncio.wait_for(anext(stream), 2)
            self.assertEqual(first, {"explanation": INITIAL})
            await asyncio.wait_for(runner.started.wait(), 2)
            self.assertFalse(runner.release.is_set())
            self.assertEqual(self.artifacts.committed, [])
            self.assertEqual(runner.calls_for("coordinator", "respond"), [])
            runner.release.set()
            final = await asyncio.wait_for(anext(stream), 2)
            output = final["output"]
            validate(output, RESPONSE_SCHEMA)
            self.assertTrue(output["message"].startswith(INITIAL + "\n\n" + SUPPLEMENT))
            self.assertEqual(output["message"].count(INITIAL), 1)
            self.assertEqual(final["state"]["last_response"], output)
            self.assertEqual(len(self.artifacts.committed), 1)
            visual = runner.calls_for("visualizer")[0]
            self.assertEqual(visual["initial_explanation"], INITIAL)
            self.assertEqual(visual["visual_source"], "local")
            self.assertEqual(runner.calls_for("coordinator", "respond")[0]["initial_explanation"], INITIAL)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        finally:
            runner.release.set()
            await stream.aclose()

    async def test_onboarding_closing_is_part_of_the_early_prefix_exactly_once(self):
        state = {"onboarding": {"step": "consent_self", "who": "self", "stage": "大学",
                                "pending_question": USER}}
        runner = FakeRunner([visual_decision()], replies=[answer()])
        events = [event async for event in self.pipeline(runner).run_stream(
            messages("先不保存"), session_id="onboarding", state=state)]
        closing = "好，这次不保存学习记录。想保存时随时说“开启记录”。现在可以问问题了。"
        # The program's closing words go out at once, before any model call.
        self.assertEqual(events[0], {"delta": closing + "\n\n"})
        self.assertEqual(events[1], {"explanation": closing + "\n\n" + INITIAL})
        output = events[-1]["output"]
        self.assertTrue(output["message"].startswith(events[1]["explanation"]))
        self.assertEqual(output["message"].count(closing), 1)
        self.assertEqual(output["message"].count(INITIAL), 1)
        self.assertEqual(events[-1]["state"]["onboarding"]["step"], "done")
        self.assertEqual(events[-1]["state"]["onboarding"]["records"], "off")
        self.assertEqual(runner.calls_for("coordinator", "decide")[0]["messages"][-1]["text"], USER)

    async def test_repeated_exact_explanation_is_not_duplicated_in_final(self):
        runner = FakeRunner([visual_decision()], replies=[answer(INITIAL + "\n\n" + SUPPLEMENT)])
        events = [event async for event in self.pipeline(runner).run_stream(messages(USER), session_id="dedup", returning_visitor=True)]
        self.assertEqual(events[0], {"explanation": INITIAL})
        self.assertEqual(events[-1]["output"]["message"].count(INITIAL), 1)
        self.assertEqual(events[-1]["output"]["message"].count(SUPPLEMENT), 1)

    async def test_visual_failure_keeps_initial_explanation_and_reports_no_image(self):
        runner = FakeRunner([visual_decision()], visuals=[StageFailure("visualizer_unavailable")],
                            replies=[answer("图示这次没有生成，可以先按上述两种数量理解。")])
        events = [event async for event in self.pipeline(runner).run_stream(messages(USER), session_id="visual-fail", returning_visitor=True)]
        self.assertEqual(events[0], {"explanation": INITIAL})
        output = events[-1]["output"]
        self.assertTrue(output["message"].startswith(INITIAL + "\n\n"))
        self.assertEqual(output["message"].count(INITIAL), 1)
        self.assertIn("未生成可用的互动图", output["message"])
        self.assertEqual(events[-1]["state"]["last_execution"]["tools"]["visual"], "unavailable")
        self.assertEqual(self.artifacts.committed, [])

    async def test_final_response_failure_keeps_delivered_text_without_state_or_artifact_commit(self):
        baseline = {"revision": 2}
        runner = FakeRunner([visual_decision()], replies=[StageFailure("respond_unavailable")])
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            events = [event async for event in self.pipeline(runner).run_stream(
                messages(USER), session_id="respond-fail", returning_visitor=True, state=baseline)]
        self.assertEqual(events[0], {"explanation": INITIAL})
        self.assertTrue(events[-1]["output"]["message"].startswith(INITIAL + "\n\n"))
        self.assertIn("后续图示未能完成", events[-1]["output"]["message"])
        self.assertEqual(events[-1]["state"], baseline)
        self.assertEqual(self.artifacts.committed, [])

    async def test_waiting_quiz_suppresses_initial_explanation(self):
        waiting = {"topic": "分数", "total": 3, "difficulty": "简单", "asked": 1, "answered": 0,
                   "correct": 0, "awaiting": deepcopy(QUIZ), "finished": False,
                   "participant": "learner", "kind": "practice", "errors": []}
        runner = FakeRunner([visual_decision()], replies=[answer()])
        events = [event async for event in self.pipeline(runner).run_stream(
            messages(USER), session_id="waiting", state={"quiz": waiting}, returning_visitor=True)]
        self.assertEqual(len(events), 1)
        self.assertEqual(runner.calls_for("coordinator", "respond")[0]["initial_explanation"], None)
        self.assertEqual(events[0]["state"]["quiz"]["awaiting"], QUIZ)
        self.assertNotIn(QUIZ["choices"][QUIZ["answer_index"]], events[0]["output"]["message"])

    async def test_pending_research_never_delivers_initial_explanation(self):
        research = BlockedResearch()
        runner = FakeRunner([visual_decision(research=True)], replies=[answer()])
        stream = self.pipeline(runner, research).run_stream(
            messages(USER), session_id="research", external_permissions={"research"}, returning_visitor=True)
        next_event = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(research.started.wait(), 2)
            self.assertFalse(next_event.done(), "No stable explanation may bypass this turn's research")
            research.release.set()
            event = await asyncio.wait_for(next_event, 2)
            self.assertIn("output", event)
            self.assertNotIn("explanation", event)
            self.assertIsNone(runner.calls_for("coordinator", "respond")[0]["initial_explanation"])
            self.assertIsNone(runner.calls_for("visualizer")[0]["initial_explanation"])
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        finally:
            research.release.set()
            if not next_event.done():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
            await stream.aclose()

    async def test_non_scene_observation_task_survives_check_offer(self):
        decision = visual_decision(check={"topic": "分子与分母", "difficulty": "简单"})
        runner = FakeRunner([decision], replies=[answer()])
        output, state = await self.pipeline(runner).run(messages(USER), session_id="observation")
        self.assertEqual(self.artifacts.committed[0][1]["spec"]["kind"], "comparison")
        self.assertEqual(output["next_step"], OBSERVATION)
        self.assertNotIn("check_offer", state)
        self.assertEqual(runner.calls_for("quizzer"), [])

    async def test_closing_stream_joins_cancelled_worker_without_committing(self):
        runner = BlockedVisualRunner([visual_decision()], replies=[answer()])
        runner.delay_cleanup = True
        pipeline = self.pipeline(runner)
        baseline = {"revision": 2}
        stream = pipeline.run_stream(messages(USER), session_id="cancel", returning_visitor=True, state=baseline, turn_id="turn")
        closing = None
        try:
            self.assertEqual(await asyncio.wait_for(anext(stream), 2), {"explanation": INITIAL})
            await asyncio.wait_for(runner.started.wait(), 2)
            closing = asyncio.create_task(stream.aclose())
            await asyncio.wait_for(runner.cancelled.wait(), 2)
            self.assertFalse(closing.done(), "The generator must await child cleanup before returning")
            runner.cleanup_release.set()
            await asyncio.wait_for(closing, 2)
            self.assertTrue(runner.exited.is_set())
            self.assertEqual(self.artifacts.committed, [])
            self.assertEqual(self.store.snapshot("cancel")["concepts"], [])
            self.assertEqual(baseline, {"revision": 2})
            self.assertNotIn("cancel", pipeline._recent)
            self.assertNotIn(("cancel", "turn"), pipeline._accepted)
            self.assertEqual(runner.calls_for("coordinator", "respond"), [])
        finally:
            runner.cleanup_release.set()
            runner.release.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            else:
                await stream.aclose()


if __name__ == "__main__":
    unittest.main()
