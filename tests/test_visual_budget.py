"""Optional visual work cannot consume response retries or discard quiz feedback."""

import asyncio
from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import (
    FakeArtifacts,
    FakeResearch,
    FakeRunner,
    LearningPipeline,
    LearningStore,
    QUIZ,
    RESPONSE_SCHEMA,
    messages,
    plan,
)
from learning_coach.adapters.runner import ROLES, StructuredRunner
from learning_coach.domain.contracts import object_schema
from learning_coach.service.execution import (
    BUDGET,
    CALL_RESERVE,
    CallBudget,
    StageFailure,
)
from learning_coach.service.visual_generation import VisualGenerationService


class SDKAgent:
    def __init__(
        self, role, scripted, *, invalid_response_once=False, slow_visual=False
    ):
        self.role, self.scripted = role, scripted
        self.invalid_response_once = invalid_response_once
        self.slow_visual = slow_visual
        self.cancelled = False
        self.calls = 0

    async def run(self, request, **kwargs):
        self.calls += 1
        data = json.loads(request)
        payload = data["input"]
        if self.slow_visual and self.role == "visualizer":
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        if self.invalid_response_once and payload.get("phase") == "respond":
            self.invalid_response_once = False
            return SimpleNamespace(text="invalid JSON")
        output = await self.scripted(self.role, payload, data["output_schema"])
        return SimpleNamespace(text=json.dumps(output, ensure_ascii=False))


class ResponseReservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_workers_and_format_retry_cannot_take_reserved_calls(self):
        class InvalidAgent:
            calls = 0

            async def run(self, *args, **kwargs):
                self.calls += 1
                await asyncio.sleep(0)
                return SimpleNamespace(text="invalid JSON")

        agent = InvalidAgent()
        runner = StructuredRunner(dict.fromkeys(ROLES, agent))
        budget = CallBudget(remaining=3)
        token = BUDGET.set(budget)
        reserve = CALL_RESERVE.set(2)
        try:
            results = await asyncio.gather(
                runner("notes", {}, object_schema({"ok": {"type": "boolean"}})),
                runner("visualizer", {}, object_schema({"ok": {"type": "boolean"}})),
                return_exceptions=True,
            )
            self.assertTrue(all(isinstance(item, StageFailure) for item in results))
            self.assertEqual(agent.calls, 1)
            self.assertEqual(budget.remaining, 2)
        finally:
            CALL_RESERVE.reset(reserve)
            BUDGET.reset(token)


class QuizVisualFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "memory.sqlite3")
        self.artifacts = FakeArtifacts()
        self.state = {
            "quiz": {
                "topic": "分数",
                "total": 1,
                "difficulty": "简单",
                "asked": 1,
                "answered": 0,
                "correct": 0,
                "awaiting": deepcopy(QUIZ),
                "finished": False,
                "question_history": [QUIZ["question"]],
                "participant": "learner",
            }
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def build(self, *, invalid_response_once=False, slow_visual=False, notes=False):
        scripted = FakeRunner(
            [
                plan(
                    "C",
                    quiz_action="continue",
                    quiz_topic="分数",
                    visual_request="用图解释这道题",
                    observe_learning=notes,
                )
            ]
        )
        agents = {
            role: SDKAgent(
                role,
                scripted,
                invalid_response_once=invalid_response_once and role == "coordinator",
                slow_visual=slow_visual,
            )
            for role in ROLES
        }
        pipeline = LearningPipeline(
            StructuredRunner(agents),
            FakeResearch(),
            self.store,
            self.artifacts,
            RESPONSE_SCHEMA,
        )
        return pipeline, scripted, agents

    async def test_low_budget_preserves_both_response_attempts_and_quiz_grade(self):
        self.store.set_enabled("learner", True)
        pipeline, scripted, agents = self.build(invalid_response_once=True, notes=True)
        with patch(
            "learning_coach.service.pipeline.shell.CallBudget",
            return_value=CallBudget(remaining=3),
        ):
            output, state = await pipeline.run(
                messages(QUIZ["choices"][0]),
                session_id="learner",
                state=self.state,
                turn_id="grade",
            )
        self.assertEqual(output["intent"]["primary"], "C")
        self.assertTrue(state["quiz"]["finished"])
        self.assertEqual(state["quiz"]["correct"], 1)
        self.assertEqual(state["last_execution"]["tools"]["visual"], "unavailable")
        # Learning evidence is only marked in the turn; notes are compiled later.
        self.assertEqual(state["last_execution"]["tools"]["notes"], "deferred")
        self.assertEqual(
            agents["coordinator"].calls, 3
        )  # decide, invalid respond, valid retry
        self.assertEqual(agents["visualizer"].calls, 0)
        self.assertEqual(agents["notes"].calls, 0)
        self.assertEqual(self.state["quiz"]["answered"], 0)
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(CALL_RESERVE.get(), 0)

    async def test_visual_timeout_cancels_generation_but_returns_quiz_feedback(self):
        pipeline, scripted, agents = self.build(slow_visual=True)
        with patch(
            "learning_coach.service.pipeline.teach.VisualGenerationService",
            partial(VisualGenerationService, timeout=0.01),
        ):
            output, state = await pipeline.run(
                messages(QUIZ["choices"][0]),
                session_id="learner",
                state=self.state,
                turn_id="grade",
            )
        self.assertEqual(output["intent"]["primary"], "C")
        self.assertTrue(state["quiz"]["finished"])
        self.assertEqual(state["quiz"]["correct"], 1)
        self.assertTrue(agents["visualizer"].cancelled)
        context = scripted.calls_for("coordinator", "respond")[0]["visual_context"]
        self.assertEqual(context["failure"]["code"], "visual_timeout")
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(CALL_RESERVE.get(), 0)

    async def test_external_cancellation_is_not_misreported_as_visual_timeout(self):
        started = asyncio.Event()

        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()

        service = VisualGenerationService(blocked, self.artifacts)
        task = asyncio.create_task(
            service.generate("learner", {"goal": "解释变化"}, learner_text="演示变化")
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.artifacts.committed, [])


if __name__ == "__main__":
    unittest.main()
