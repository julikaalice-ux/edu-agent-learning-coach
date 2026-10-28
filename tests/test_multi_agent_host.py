"""Actual locked SDK boundaries with fake model/pipeline; no credentials/network."""

import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(SOURCE))
SDK_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in
                    ("agent_framework", "agent_framework_foundry_hosting", "jsonschema"))
if SDK_AVAILABLE:
    from agent_framework import (Agent, AgentResponse, AgentSession, ChatResponse, ChatResponseUpdate, Content,
                                 Message, ResponseStream)
    from agent_framework_foundry_hosting import ResponsesHostServer
    from azure.ai.agentserver.responses import InMemoryResponseProvider
    from starlette.requests import Request
    from learning_coach.domain.contracts import validate
    from learning_coach.api.host import GUARD_KEY, LearningCoachAgent
    from learning_coach.db.memory import LearningStore
    from learning_coach.service.pipeline import LearningPipeline
    from learning_coach.service.research import ResearchService
    from learning_coach.adapters.runner import BUDGET, ROLES, StageFailure, StructuredRunner, create_agents
    from learning_coach.service.execution import CallBudget
    from learning_coach.service.pipeline.texts import INTRO, INTRO_RECORDS_OFF
    from learning_coach.adapters.visuals import VISUAL_SCHEMA, render_visual
    from learning_coach.db.artifacts import ArtifactStore

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
FINAL_SCHEMA = json.loads((SOURCE / "response.schema.json").read_text(encoding="utf-8"))
FINAL = {"schema_version": "1.0", "intent": {"primary": "A", "is_implicit": False},
         "message": "把一个整体平均分成相同大小的几份。", "next_step": {"kind": "task", "text": "画出两份中的一份。"}}


class FakeAgent:
    def __init__(self, responses=None, delay=0, error=None):
        self.responses = list(responses or ['{"ok":true}'])
        self.delay = delay
        self.error = error
        self.calls = []

    async def run(self, message, **kwargs):
        self.calls.append((message, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.responses[min(len(self.calls) - 1, len(self.responses) - 1)])


def chat_reply(text, stream, size=7):
    """What a chat client returns: a response to await, or a stream of small text pieces."""
    if not stream:
        async def whole():
            return ChatResponse(messages=[Message("assistant", [text])])
        return whole()

    async def pieces():
        for start in range(0, len(text), size):
            yield ChatResponseUpdate(contents=[Content.from_text(text[start:start + size])], role="assistant",
                                     message_id="reply")
    return ResponseStream(pieces(), finalizer=ChatResponse.from_updates)


class FakeChatClient:
    def __init__(self, output=None):
        self.calls = []
        self.output = output or {"ok": True}

    def get_response(self, messages, *, stream=False, options=None, **kwargs):
        self.calls.append((messages, stream, options))
        return chat_reply(json.dumps(self.output), stream)


class FakePipeline:
    def __init__(self):
        self.calls = []
        self.resets = []
        self.artifacts = SimpleNamespace(get=lambda token: "<!doctype html><p>教学图</p>" if token == "valid" else None)

    def reset_session(self, session_id):
        self.resets.append(session_id)

    async def run(self, inputs, *, session_id, state, learner_id=None):
        self.calls.append((deepcopy(inputs), session_id, deepcopy(state), learner_id))
        state.setdefault("seen", []).append(inputs[-1]["text"] if inputs else "")
        # Internal material is state only; it must never become a host update.
        state["internal_note"] = "PRIVATE_SPECIALIST_OUTPUT"
        return validate(deepcopy(FINAL), FINAL_SCHEMA), state


@unittest.skipUnless(SDK_AVAILABLE, "Run with the project's locked SDK environment")
class StructuredRunnerTests(unittest.IsolatedAsyncioTestCase):
    def make_runner(self, fake, **kwargs):
        agents = {role: FakeAgent() for role in ROLES}
        agents["coordinator"] = fake
        kwargs.setdefault("retry_delay", 0)
        return StructuredRunner(agents, **kwargs)

    async def test_bad_json_retries_once_with_schema_correction(self):
        fake = FakeAgent(["not JSON", '{"ok":true}'])
        result = await self.make_runner(fake)("coordinator", {"question": "分数"}, SCHEMA)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(fake.calls), 2)
        first, second = (json.loads(call[0]) for call in fake.calls)
        self.assertEqual(first["output_schema"], SCHEMA)
        self.assertNotIn("format_correction", first)
        self.assertIn("format_correction", second)
        self.assertEqual(second["input"], first["input"])

    async def test_two_invalid_outputs_fail_without_third_call(self):
        fake = FakeAgent(['{"ok":"wrong type"}', '```json\n{"ok":true}\n```'])
        with self.assertRaisesRegex(StageFailure, "invalid_stage_output"):
            await self.make_runner(fake)("coordinator", {}, SCHEMA)
        self.assertEqual(len(fake.calls), 2)

    async def test_observer_receives_every_stage_record(self):
        records = []
        runner = self.make_runner(FakeAgent(), observer=records.append)
        await runner("coordinator", {"phase": "decide", "question": "分数"}, SCHEMA)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["role"], "coordinator")
        self.assertEqual(records[0]["phase"], "decide")
        self.assertEqual(records[0]["outcome"], "ok")
        self.assertIn("elapsed_ms", records[0])

    async def test_timeout_is_bounded_and_retried_once(self):
        fake = FakeAgent(delay=10)
        with self.assertRaisesRegex(StageFailure, "provider_or_timeout_failure"):
            await self.make_runner(fake, timeout=.01)("coordinator", {}, SCHEMA)
        self.assertEqual(len(fake.calls), 2)

    async def test_a_connection_never_made_gets_one_more_try(self):
        class ConnectTimeout(Exception):
            pass

        class Unreachable(FakeAgent):
            def __init__(self, failures):
                super().__init__()
                self.failures = failures

            async def run(self, message, **kwargs):
                self.calls.append((message, kwargs))
                if len(self.calls) <= self.failures:
                    raise RuntimeError("request failed") from ConnectTimeout()
                return SimpleNamespace(text='{"ok":true}')

        flaky = Unreachable(2)
        self.assertEqual(await self.make_runner(flaky)("coordinator", {"phase": "decide"}, SCHEMA), {"ok": True})
        self.assertEqual(len(flaky.calls), 3)
        down = Unreachable(10)
        with self.assertRaises(StageFailure):
            await self.make_runner(down)("coordinator", {"phase": "decide"}, SCHEMA)
        self.assertEqual(len(down.calls), 3)

    async def test_a_hanging_picture_review_is_cut_short_and_tried_once_more(self):
        class HangsOnce(FakeAgent):
            async def run(self, message, **kwargs):
                self.calls.append((message, kwargs))
                if len(self.calls) == 1:
                    await asyncio.sleep(10)
                return SimpleNamespace(text='{"ok":true}')

        agents = {role: FakeAgent() for role in ROLES}
        agents["verifier"] = HangsOnce()
        runner = StructuredRunner(agents, retry_delay=0)
        with patch.dict("learning_coach.adapters.runner.PHASE_TIMEOUTS", {"visual_review": .05}):
            self.assertEqual(await runner("verifier", {"phase": "visual_review"}, SCHEMA), {"ok": True})
        self.assertEqual(len(agents["verifier"].calls), 2)
        # A research check by the same role is still not tried again after a timeout.
        agents["verifier"] = FakeAgent(delay=10)
        runner = StructuredRunner(agents, retry_delay=0, role_timeouts={"verifier": .05})
        with self.assertRaises(StageFailure):
            await runner("verifier", {"sources": []}, SCHEMA)
        self.assertEqual(len(agents["verifier"].calls), 1)

    async def test_one_passing_provider_error_is_retried(self):
        class Flaky(FakeAgent):
            async def run(self, message, **kwargs):
                self.calls.append((message, kwargs))
                if len(self.calls) == 1:
                    raise ConnectionError("PRIVATE_NETWORK_DETAIL")
                return SimpleNamespace(text='{"ok":true}', usage_details={
                    "input_token_count": 120, "output_token_count": 5, "cache_read_input_token_count": 64})
        fake = Flaky()
        runner = self.make_runner(fake)
        self.assertEqual(await runner("coordinator", {"phase": "decide"}, SCHEMA), {"ok": True})
        self.assertEqual(len(fake.calls), 2)
        # Usage reported by the API is kept per call, next to the local estimate.
        last = runner.usage[-1]
        self.assertEqual((last["outcome"], last["input_token_count"], last["cache_read_input_token_count"]),
                         ("ok", 120, 64))
        self.assertGreater(last["estimated_input_tokens"], 0)
        self.assertEqual(runner.usage[0]["outcome"], "provider_or_timeout_failure")

    async def test_unchanging_schema_comes_before_the_input(self):
        fake = FakeAgent()
        await self.make_runner(fake)("coordinator", {"phase": "decide", "messages": []}, SCHEMA)
        self.assertEqual(list(json.loads(fake.calls[0][0])), ["output_schema", "input"])

    async def test_provider_failure_has_no_private_exception_text(self):
        fake = FakeAgent(error=RuntimeError("secret-token learner-private-text"))
        with self.assertLogs("learning_coach.adapters.runner", level="WARNING") as logs:
            with self.assertRaisesRegex(StageFailure, "provider_or_timeout_failure") as raised:
                await self.make_runner(fake)("coordinator", {}, SCHEMA)
        self.assertNotIn("secret-token", " ".join(logs.output) + str(raised.exception))
        self.assertNotIn("learner-private-text", " ".join(logs.output))

    async def test_stage_logs_record_retry_outcome_without_payload_or_response(self):
        fake = FakeAgent(["PRIVATE_BAD_MODEL_OUTPUT", '{"ok":true}'])
        with self.assertLogs("learning_coach.adapters.runner", level="INFO") as logs:
            await self.make_runner(fake)("coordinator", {"phase": "decide", "question": "PRIVATE_LEARNER_INPUT"}, SCHEMA)
        stages = [record.getMessage() for record in logs.records if record.getMessage().startswith("agent_stage ")]
        self.assertEqual(len(stages), 2)
        self.assertRegex(stages[0], r"role=coordinator phase=decide attempt=1 outcome=invalid_output "
                                    r"estimated_input_tokens=\d+ .* elapsed_ms=\d+ invalid_at=invalid_json$")
        self.assertRegex(stages[1], r"role=coordinator phase=decide attempt=2 outcome=ok "
                                    r"estimated_input_tokens=\d+ .* elapsed_ms=\d+$")
        self.assertNotIn("PRIVATE", " ".join(logs.output))
        failure = FakeAgent(error=RuntimeError("PRIVATE_PROVIDER_SECRET"))
        with self.assertLogs("learning_coach.adapters.runner", level="INFO") as errors:
            with self.assertRaises(StageFailure):
                await self.make_runner(failure)("coordinator", {"phase": "PRIVATE_PHASE"}, SCHEMA)
        self.assertNotIn("PRIVATE", " ".join(errors.output))
        self.assertTrue(any("phase=specialist attempt=1 outcome=provider_or_timeout_failure" in line for line in errors.output))

    async def test_failed_call_records_the_error_chain_as_types_only(self):
        class APIConnectionError(Exception):
            pass
        try:
            try:
                raise ConnectionError("PRIVATE_HOST_DETAIL")
            except ConnectionError as cause:
                raise APIConnectionError("PRIVATE_REQUEST") from cause
        except APIConnectionError as error:
            chained = error
        records = []
        with self.assertLogs("learning_coach.adapters.runner", level="WARNING") as logs:
            with self.assertRaises(StageFailure):
                await self.make_runner(FakeAgent(error=chained), observer=records.append)("coordinator", {}, SCHEMA)
        self.assertEqual(records[0]["error"], "APIConnectionError<ConnectionError")
        self.assertIn("error=APIConnectionError<ConnectionError", " ".join(logs.output))
        self.assertNotIn("PRIVATE", " ".join(logs.output) + json.dumps(records))

    async def test_zero_budget_prevents_model_call(self):
        fake = FakeAgent()
        budget = CallBudget(remaining=0)
        token = BUDGET.set(budget)
        try:
            with self.assertRaisesRegex(StageFailure, "call_budget_exhausted"):
                await self.make_runner(fake)("coordinator", {}, SCHEMA)
        finally:
            BUDGET.reset(token)
        self.assertEqual(fake.calls, [])

    async def test_retry_consumes_shared_budget(self):
        fake = FakeAgent(["bad", '{"ok":true}'])
        budget = CallBudget(remaining=1)
        token = BUDGET.set(budget)
        try:
            with self.assertRaisesRegex(StageFailure, "call_budget_exhausted"):
                await self.make_runner(fake)("coordinator", {}, SCHEMA)
        finally:
            BUDGET.reset(token)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(budget.remaining, 0)

    async def test_native_output_schema_passed_to_sdk_options(self):
        fake = FakeAgent(['{"result":{"ok":true}}'])
        result = await self.make_runner(fake, native_json=True)("coordinator", {}, SCHEMA)
        self.assertEqual(result, {"ok": True})
        wrapped = fake.calls[0][1]["options"]["response_format"]
        self.assertEqual(wrapped["type"], "object")
        self.assertEqual(wrapped["properties"]["result"], SCHEMA)
        self.assertEqual(json.loads(fake.calls[0][0])["output_schema"], wrapped)

    async def test_prompt_only_output_keeps_local_validation(self):
        fake = FakeAgent(['{"ok":true,"unexpected":1}', '{"ok":true}'])
        await self.make_runner(fake, native_json=False)("coordinator", {}, SCHEMA)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0][1], {"options": {}})
        self.assertNotIn("session", fake.calls[0][1])

    async def test_unknown_role_never_calls_model(self):
        fake = FakeAgent()
        with self.assertRaisesRegex(StageFailure, "unknown_role"):
            await self.make_runner(fake)("invented", {}, SCHEMA)
        self.assertEqual(fake.calls, [])

    def test_runner_requires_exactly_the_known_roles(self):
        with self.assertRaisesRegex(ValueError, "agent_roles_mismatch"):
            StructuredRunner({"coordinator": FakeAgent()})


@unittest.skipUnless(SDK_AVAILABLE, "Run with the project's locked SDK environment")
class AgentFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_real_agents_load_prompts_and_accept_fake_client(self):
        client = FakeChatClient()
        agents = create_agents(client, SOURCE / "prompts/instructions.md")
        prompt_paths = {"coordinator": SOURCE / "prompts/instructions.md",
                        "verifier": SOURCE / "prompts/agents/source-check.md",
                        "notes": SOURCE / "prompts/agents/note-organizer.md",
                        "visualizer": SOURCE / "prompts/agents/visual-designer.md",
                        "quizzer": SOURCE / "prompts/agents/quiz-writer.md"}
        self.assertEqual(set(agents), set(ROLES))
        self.assertEqual(len({id(agent) for agent in agents.values()}), 5)
        for role, agent in agents.items():
            with self.subTest(role=role):
                self.assertIsInstance(agent, Agent)
                self.assertEqual(agent.name, role)
                result = await agent.run("offline construction check")
                self.assertEqual(json.loads(result.text), {"ok": True})
                options = client.calls[-1][2]
                self.assertIn(prompt_paths[role].read_text(encoding="utf-8"), options["instructions"])
                self.assertIs(options["store"], False)
        self.assertEqual(len(client.calls), 5)

    async def test_reasoning_effort_per_role_reaches_only_that_agents_requests(self):
        from learning_coach.server import role_options_from_env
        with patch.dict("os.environ", {"LEARNING_COACH_REASONING_VISUALIZER": "Low",
                                       "LEARNING_COACH_REASONING_VERIFIER": "fast"}):
            with self.assertLogs("learning_coach.server", level="WARNING"):
                options = role_options_from_env()
        self.assertEqual(options, {"visualizer": {"reasoning": {"effort": "low"}}})  # an unknown value is ignored
        client = FakeChatClient()
        agents = create_agents(client, SOURCE / "prompts/instructions.md", role_options=options)
        await agents["visualizer"].run("draw")
        self.assertEqual(client.calls[-1][2]["reasoning"], {"effort": "low"})
        await agents["coordinator"].run("decide")
        self.assertNotIn("reasoning", client.calls[-1][2])

    async def test_native_visual_union_is_wrapped_and_returns_original_spec(self):
        visual = {"kind": "flashcards", "title": "分数", "cards": [{"front": "一半", "back": "二分之一"}]}
        client = FakeChatClient({"result": visual})
        runner = StructuredRunner(create_agents(client, SOURCE / "prompts/instructions.md"), native_json=True)
        result = await runner("visualizer", {"topic": "分数"}, VISUAL_SCHEMA)
        self.assertEqual(result, visual)
        options = client.calls[-1][2]
        wrapped = options["response_format"]
        self.assertEqual(wrapped["type"], "object")
        self.assertIn("anyOf", wrapped["properties"]["result"])
        self.assertNotIn('"oneOf"', json.dumps(wrapped))
        validate(result, VISUAL_SCHEMA)


@unittest.skipUnless(SDK_AVAILABLE, "Run with the project's locked SDK environment")
class LearningCoachHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_and_nonstream_have_one_identical_final_result(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        session = AgentSession()
        plain = await agent.run("分数是什么", session=session)
        stream = agent.run("分数是什么", stream=True, session=AgentSession())
        updates = [update async for update in stream]
        final = await stream.get_final_response()
        self.assertIsInstance(plain, AgentResponse)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].text, plain.text)
        self.assertEqual(final.text, plain.text)
        self.assertEqual(final.text, FINAL["message"] + "\n\n" + FINAL["next_step"]["text"])
        self.assertEqual(session.state["learning_coach"]["last_response"], FINAL)
        self.assertNotIn("PRIVATE_SPECIALIST_OUTPUT", final.text)
        self.assertEqual(len(pipeline.calls), 2)

    async def test_streamed_pieces_and_the_final_reply_read_as_one_message(self):
        full = FINAL["message"] + "\n\n" + FINAL["next_step"]["text"]

        class StreamingPipeline(FakePipeline):
            def __init__(self, events):
                super().__init__()
                self.events = events

            async def run_stream(self, inputs, *, session_id, state, learner_id=None):
                for event in self.events:
                    yield event
                state["streamed"] = True
                yield {"output": validate(deepcopy(FINAL), FINAL_SCHEMA), "state": state}

        cases = {
            "the final reply continues the draft": (
                [{"delta": "把一个整体"}, {"delta": "平均分成"}],
                [full]),
            "an early explanation, then the rest": (
                [{"explanation": "把一个整体平均分成相同大小的几份。"}],
                ["把一个整体平均分成相同大小的几份。", "\n\n" + FINAL["next_step"]["text"]]),
            "a draft the program replaced": (
                [{"delta": "草稿"}],
                [full]),
        }
        for name, (events, expected) in cases.items():
            with self.subTest(name):
                agent = LearningCoachAgent(StreamingPipeline(events))
                session = AgentSession()
                stream = agent.run("分数是什么", stream=True, session=session)
                updates = [update async for update in stream]
                final = await stream.get_final_response()
                self.assertEqual([update.text for update in updates], expected)
                self.assertEqual(len({update.message_id for update in updates}), 1)
                self.assertEqual(final.text, "".join(expected))
                self.assertEqual(session.state["learning_coach"]["last_response"], FINAL)
                self.assertTrue(session.state["learning_coach"]["streamed"])

    async def test_two_sdk_sessions_do_not_share_state(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        first, second = agent.create_session(), agent.create_session()
        self.assertNotEqual(first.session_id, second.session_id)
        await agent.run("first", session=first)
        await agent.run("second", session=second)
        await agent.run("first continued", session=first)
        self.assertEqual(first.state["learning_coach"]["seen"], ["first", "first continued"])
        self.assertEqual(second.state["learning_coach"]["seen"], ["second"])
        self.assertEqual(pipeline.calls[1][2], {})
        self.assertEqual(pipeline.calls[2][2]["seen"], ["first"])

    async def test_pipeline_failure_does_not_mutate_session_or_emit_update(self):
        class FailingPipeline:
            async def run(self, inputs, *, session_id, state, learner_id=None):
                state.setdefault("seen", []).append("uncommitted")
                raise RuntimeError("failed before final result")
        agent = LearningCoachAgent(FailingPipeline())
        session = agent.create_session()
        session.state["learning_coach"] = {"seen": ["previous"]}
        emitted = []
        with self.assertRaisesRegex(RuntimeError, "failed before final result"):
            async for update in agent.run("next", stream=True, session=session):
                emitted.append(update)
        self.assertEqual(emitted, [])
        self.assertEqual(session.state["learning_coach"], {"seen": ["previous"]})

    async def test_message_objects_preserve_roles_and_text(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        session = agent.create_session()
        # A continuing thread: the guard is recent, so the full history passes through.
        session.state[GUARD_KEY] = {"owner": None, "last_turn_at": time.time(), "skip": 0}
        await agent.run([Message("assistant", ["prior"]), Message("user", ["current"])], session=session)
        self.assertEqual(pipeline.calls[0][0], [{"role": "assistant", "text": "prior"},
                                               {"role": "user", "text": "current"}])

    async def test_single_message_object_is_accepted(self):
        pipeline = FakePipeline()
        await LearningCoachAgent(pipeline).run(Message("user", ["current"]))
        self.assertEqual(pipeline.calls[0][0], [{"role": "user", "text": "current"}])

    async def test_platform_user_id_becomes_learner_id(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        context = SimpleNamespace(user_id="user-123")
        with patch("learning_coach.api.host.get_request_context", return_value=context), \
                patch.dict("os.environ", {"LEARNING_COACH_RECORDS_SCOPE": "user"}):
            await agent.run("你好", session=AgentSession())
        self.assertEqual(pipeline.calls[0][3], "user-123")

    async def test_missing_or_absent_platform_context_passes_no_learner_id(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        with patch("learning_coach.api.host.get_request_context",
                   return_value=SimpleNamespace(user_id=None)):
            await agent.run("你好", session=AgentSession())
        with patch("learning_coach.api.host.get_request_context",
                   side_effect=RuntimeError("no request context")):
            await agent.run("你好", session=AgentSession())
        self.assertEqual([call[3] for call in pipeline.calls], [None, None])

    async def test_first_turn_has_no_guard_and_starts_fresh(self):
        # A brand-new session carries nothing, so even a replayed history is ignored.
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        session = agent.create_session()
        await agent.run([Message("user", ["old"]), Message("user", ["current"])], session=session)
        self.assertEqual(pipeline.calls[0][0], [{"role": "user", "text": "current"}])
        self.assertEqual(pipeline.calls[0][2], {})
        self.assertEqual(pipeline.resets, [session.session_id])
        self.assertEqual(session.state[GUARD_KEY]["skip"], 1)

    async def test_owner_change_resets_context_and_keeps_old_thread_out_of_later_turns(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        session = agent.create_session()
        with patch("learning_coach.api.host.get_request_context", return_value=SimpleNamespace(user_id="user-a")):
            await agent.run([Message("user", ["a1"])], session=session)
            await agent.run([Message("user", ["a1"]), Message("assistant", ["a2"]), Message("user", ["a3"])],
                            session=session)
        self.assertEqual(len(pipeline.calls[1][0]), 3)
        # Someone else opens the same conversation link.
        with patch("learning_coach.api.host.get_request_context", return_value=SimpleNamespace(user_id="user-b")):
            await agent.run([Message("user", ["a1"]), Message("assistant", ["a2"]), Message("user", ["a3"]),
                             Message("assistant", ["a4"]), Message("user", ["b1"])], session=session)
        self.assertEqual(pipeline.calls[2][0], [{"role": "user", "text": "b1"}])
        self.assertEqual(pipeline.calls[2][2], {})
        self.assertEqual(pipeline.resets, [session.session_id] * 2)  # first turn + owner change
        self.assertNotEqual(pipeline.calls[2][3], pipeline.calls[1][3])  # user-b never reads user-a's records
        self.assertEqual(session.state[GUARD_KEY]["owner"], "user-b")
        # The next turn in the new owner's page-open keeps only the post-reset
        # messages; the old owner's part of the thread never comes back.
        with patch("learning_coach.api.host.get_request_context", return_value=SimpleNamespace(user_id="user-b")):
            await agent.run([Message("user", ["a1"]), Message("assistant", ["a2"]), Message("user", ["a3"]),
                             Message("assistant", ["a4"]), Message("user", ["b1"]),
                             Message("assistant", ["b2"]), Message("user", ["b3"])], session=session)
        self.assertEqual([item["text"] for item in pipeline.calls[3][0]], ["b1", "b2", "b3"])
        self.assertNotIn("a", " ".join(item["text"] for item in pipeline.calls[3][0]))

    async def test_ttl_expiry_resets_context_and_zero_disables_time_reset(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        session = agent.create_session()
        t0 = time.time()
        with patch("learning_coach.api.host.get_request_context", return_value=SimpleNamespace(user_id="user-a")), \
             patch("learning_coach.api.host.time.time", return_value=t0):
            await agent.run([Message("user", ["hello"])], session=session)
        with patch("learning_coach.api.host.get_request_context", return_value=SimpleNamespace(user_id="user-a")), \
             patch("learning_coach.api.host.time.time", return_value=t0 + 901):
            await agent.run([Message("user", ["hello"]), Message("assistant", ["hi"]),
                             Message("user", ["again"])], session=session)
        self.assertEqual(pipeline.calls[1][0], [{"role": "user", "text": "again"}])
        self.assertEqual(pipeline.calls[1][2], {})
        self.assertEqual(len(pipeline.resets), 2)  # first turn + TTL expiry
        # TTL 0 turns time-based reset off: the same owner continues.
        with patch("learning_coach.api.host._context_ttl_seconds", return_value=0.0), \
             patch("learning_coach.api.host.get_request_context", return_value=SimpleNamespace(user_id="user-a")), \
             patch("learning_coach.api.host.time.time", return_value=t0 + 3600):
            await agent.run([Message("user", ["hello"]), Message("assistant", ["hi"]),
                             Message("user", ["again"]), Message("assistant", ["ok"]),
                             Message("user", ["more"])], session=session)
        self.assertEqual([item["text"] for item in pipeline.calls[2][0]], ["again", "ok", "more"])
        self.assertEqual(len(pipeline.resets), 2)  # TTL 0 adds no further reset

    async def test_guard_stays_out_of_pipeline_state(self):
        pipeline = FakePipeline()
        agent = LearningCoachAgent(pipeline)
        session = agent.create_session()
        session.state["learning_coach"] = {"earlier": "must not reach a fresh turn"}
        with patch("learning_coach.api.host.get_request_context", return_value=SimpleNamespace(user_id="user-a")):
            await agent.run("你好", session=session)
        self.assertEqual(pipeline.calls[0][2], {})
        self.assertEqual(session.state[GUARD_KEY]["owner"], "user-a")


@unittest.skipUnless(SDK_AVAILABLE, "Run with the project's locked SDK environment")
class ResponsesRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_server_uses_memory_store_and_serves_artifact_route(self):
        spec = importlib.util.spec_from_file_location("main_for_host_tests", SOURCE / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pipeline = FakePipeline()
        # Neither creating the server nor reading an artifact should create an SDK
        # transcript directory, load credentials, or start telemetry exporters.
        with patch("azure.ai.agentserver.responses.store._file.FileResponseStore",
                   side_effect=AssertionError("unexpected transcript persistence")), \
             patch("learning_coach.server.DefaultAzureCredential", side_effect=AssertionError("unexpected credentials")), \
             patch("learning_coach.api.http.ResponsesHostServer", wraps=ResponsesHostServer) as factory:
            server = module.create_server(pipeline)
        self.assertIsInstance(server, ResponsesHostServer)
        self.assertIsInstance(factory.call_args.kwargs["store"], InMemoryResponseProvider)
        self.assertIsNone(factory.call_args.kwargs["configure_observability"])
        routes = [route for route in server.routes if getattr(route, "path", None) == "/artifacts/{token}"]
        self.assertEqual(len(routes), 1)
        self.assertIn("GET", routes[0].methods)
        self.assertNotIn("POST", routes[0].methods)
        for token, expected in (("valid", 200), ("missing", 404)):
            request = Request({"type": "http", "method": "GET", "path": f"/artifacts/{token}",
                               "headers": [], "path_params": {"token": token}})
            response = await routes[0].endpoint(request)
            self.assertEqual(response.status_code, expected)
            if expected == 200:
                self.assertIn("教学图".encode(), response.body)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn("connect-src 'none'", response.headers["content-security-policy"])
        self.assertEqual(pipeline.calls, [])


@unittest.skipUnless(SDK_AVAILABLE, "Run with the project's locked SDK environment")
class FiveRoleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_direct_decision_finishes_in_one_model_call(self):
        decision = {"mode": "answer", "response": deepcopy(FINAL),
                    "task": {"task_summary": "理解分数", "pause_task": False, "end_task": False},
                    "context_notes": None, "check_question": None}
        for native in (False, True):
            with self.subTest(native=native), TemporaryDirectory() as directory:
                client = FakeChatClient({"result": decision} if native else decision)
                runner = StructuredRunner(create_agents(client, SOURCE / "prompts/instructions.md"), native_json=native)
                store = LearningStore(Path(directory) / "memory.sqlite3")
                try:
                    artifacts = ArtifactStore(Path(directory) / "artifacts", renderer=render_visual)
                    research = SimpleNamespace(run=None)
                    pipeline = LearningPipeline(runner, research, store, artifacts, FINAL_SCHEMA)
                    host = LearningCoachAgent(pipeline)
                    session = host.create_session()
                    with patch.object(artifacts, "prepare", side_effect=AssertionError("direct answer must not create a visual")):
                        response = await host.run("分数是什么意思？", session=session)
                    # A first question goes straight to the answer.
                    self.assertEqual(response.text, FINAL["message"] + "\n\n" + FINAL["next_step"]["text"])
                    self.assertEqual(session.state["learning_coach"]["last_response"],
                                     {**FINAL, "message": FINAL["message"]})
                    self.assertEqual(len(client.calls), 1)
                    request = json.loads(client.calls[0][0][-1].text)
                    self.assertEqual(request["input"]["phase"], "decide")
                    self.assertEqual(session.state["learning_coach"]["last_execution"]["mode"], "answer")
                    self.assertEqual(list(artifacts.root.glob("*.html")), [])
                finally:
                    store.close()

    async def test_real_sdk_pipeline_persists_notes_and_serves_visual(self):
        from learning_coach.service.execution import EXTERNAL_PERMISSIONS
        token = EXTERNAL_PERMISSIONS.set(frozenset({"research"}))
        self.addCleanup(EXTERNAL_PERMISSIONS.reset, token)
        class Provider:
            def __init__(self):
                self.calls = []

            async def search(self, query):
                self.calls.append(query)
                return [
                    {"title": "Water phase notes", "url": "https://first.example/water",
                     "excerpt": "At normal pressure, water occupies more volume when frozen.",
                     "excerpt_kind": "page_extract"},
                    {"title": "Ice density experiment", "url": "https://second.test/ice",
                     "excerpt": "An ice crystal has an open lattice, making ordinary ice less dense than liquid water.",
                     "excerpt_kind": "page_extract"},
                ]

        class ScriptedChatClient(FakeChatClient):
            def __init__(self):
                super().__init__()
                self.roles = []
                self.response_payload = None

            def get_response(self, messages, *, stream=False, options=None, **kwargs):
                self.calls.append((messages, stream, options))
                request = json.loads(messages[-1].text)
                data = request["input"]
                if data.get("phase") == "decide":
                    role = "coordinator"
                    answer = {"intent": FINAL["intent"], "research_query": "water freezing expansion",
                              "search_queries": ["water freezing expansion", "ordinary ice open lattice density"],
                              "visual_request": "展示水和冰的结构差异", "visual_source": "local", "observe_learning": True,
                              "task_summary": "理解水结冰体积变化", "pause_task": False, "end_task": False,
                              "quiz_action": "none", "quiz_topic": None, "quiz_total": 1,
                              "difficulty": "简单", "participant": "learner",
                              "visual_uses_research": True, "quiz_uses_research": None,
                              "quiz_uses_visual": None}
                    quote = data["messages"][-1]["text"]
                    answer["tool_reasons"] = {
                        "research": {"code": "explicit_lookup", "evidence_quote": quote}, "quiz": None,
                        "notes": {"code": "profile_update", "evidence_quote": quote},
                        "visual": {"code": "explicit_visual", "evidence_quote": quote},
                    }
                    answer = {"mode": "tools", "plan": answer, "initial_explanation": None, "context_notes": None, "check_question": None}
                elif data.get("phase") == "respond":
                    role = "coordinator"
                    self.response_payload = deepcopy(data)
                    answer = {"schema_version": "1.0", "intent": FINAL["intent"],
                              "message": "普通条件下，冰的晶体结构比液态水疏松，因此相同质量的水结冰后体积会增大。",
                              "next_step": {"kind": "question", "text": "这能解释冰为什么浮在水上吗？"}}
                elif data.get("phase") == "visual_review":
                    role = "verifier"
                    answer = {"verdict": "pass", "checks": {key: True for key in (
                        "request_match", "factual_consistency", "labels_clear", "interaction_useful", "not_misleading")},
                        "issues": [], "fact_basis": "provided_sources"}
                elif "sources" in data:
                    role = "verifier"
                    answer = {"claims": [{"text": "Water expands upon freezing at normal pressure.",
                              "status": "verified", "independent": True, "explanation": "Both excerpts directly support the claim.",
                              "evidence": [{"source_id": item["id"], "quote": item["excerpt"], "relation": "supports"}
                                           for item in data["sources"]]}]}
                elif "learner_messages" in data:
                    role = "notes"
                    answer = {"schema_version": "1.0", "profile": [{"key": "learning_stage", "value": "成人工作后学习",
                              "origin": "self_report", "evidence_quote": "我在工作后学习"}],
                              "concepts": [{"concept_id": "water-freezing", "label": "水结冰体积变化",
                              "prerequisites": None, "related": None, "evidence_quote": "水结冰为什么会膨胀"}],
                              "error_records": [], "review_plan": []}
                elif "goal" in data:
                    role = "visualizer"
                    answer = {"kind": "comparison", "title": "水与冰", "left_label": "液态水", "right_label": "普通冰",
                              "rows": [{"aspect": "结构", "left": "分子相对紧密", "right": "晶格较疏松"}]}
                else:
                    raise AssertionError("Unexpected model stage")
                self.roles.append(role)
                # This path reaches real Agent construction, message normalization,
                # option merging and response serialization, only transport is fake.
                return chat_reply(json.dumps(answer, ensure_ascii=False), stream)

        client, provider = ScriptedChatClient(), Provider()
        agents = create_agents(client, SOURCE / "prompts/instructions.md")
        runner = StructuredRunner(agents)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = LearningStore(root / "memory.sqlite3")
            try:
                artifacts = ArtifactStore(root / "artifacts", renderer=render_visual)
                pipeline = LearningPipeline(runner, ResearchService(runner, provider), store, artifacts, FINAL_SCHEMA)
                host = LearningCoachAgent(pipeline)
                session = host.create_session()
                self.assertFalse(store.is_enabled(session.session_id))
                await host.run("/memory on", session=session)
                validate(session.state["learning_coach"]["last_response"], FINAL_SCHEMA)
                self.assertTrue(store.is_enabled(session.session_id))
                self.assertEqual(client.roles, [])

                stream = host.run("我在工作后学习，想知道水结冰为什么会膨胀，请查资料并做一个对比图。",
                                  stream=True, session=session)
                updates = [update async for update in stream]
                final = await stream.get_final_response()
                output = session.state["learning_coach"]["last_response"]
                validate(output, FINAL_SCHEMA)
                expected_text = output["message"] + ("\n\n" + output["next_step"]["text"] if output["next_step"] else "")
                self.assertEqual(final.text, expected_text)
                # The reply arrives while it is written; only its text, never the JSON around it.
                self.assertGreaterEqual(len(updates), 1)
                self.assertEqual("".join(update.text for update in updates), final.text)
                # No capability pitch before the first answer.
                self.assertNotIn(INTRO, final.text)
                self.assertNotIn("“关闭记录”", final.text)
                self.assertTrue(updates[0].text.startswith("普通"))
                self.assertNotIn('"', "".join(update.text for update in updates))
                # Notes are not compiled inside the teaching turn.
                self.assertEqual(client.roles, ["coordinator", "verifier", "visualizer", "verifier", "coordinator"])
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(client.response_payload["research"]["overall"], "verified")
                self.assertEqual(client.response_payload["visual_status"], "ready")
                self.assertEqual(client.response_payload["visual_context"]["quality"]["fact_basis"], "provided_sources")
                self.assertIn("https://first.example/water", final.text)
                self.assertIn("https://second.test/ice", final.text)
                token_match = re.search(r"http://localhost:8088/artifacts/([A-Za-z0-9_-]{32})", final.text)
                self.assertIsNotNone(token_match)
                token = token_match.group(1)
                self.assertEqual(store.snapshot(session.session_id)["profile"], {})
                said = "我在工作后学习，想知道水结冰为什么会膨胀，请查资料并做一个对比图。"
                compiled = await host.run([Message("user", [said]), Message("assistant", [final.text]),
                                           Message("user", ["整理笔记"])], session=session)
                self.assertIn("整理好了", compiled.text)
                self.assertEqual(set(client.roles), set(ROLES) - {"quizzer"})  # no question was asked
                self.assertEqual(client.roles[-1], "notes")
                stored = store.snapshot(session.session_id)
                self.assertEqual(stored["profile"]["learning_stage"]["value"], "成人工作后学习")
                self.assertEqual(stored["profile"]["learning_stage"]["origin"], "self_report")
                self.assertEqual(stored["concepts"][0]["concept_id"], "water-freezing")
                self.assertIsNone(stored["concepts"][0]["mastery_level"])
                self.assertTrue((root / "memory.sqlite3").is_file())
                self.assertTrue((artifacts.root / f"{token}.html").is_file())
                self.assertIn("水与冰", artifacts.get(token))

                spec = importlib.util.spec_from_file_location("main_for_pipeline_integration", SOURCE / "main.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                server = module.create_server(pipeline)
                events = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(event):
                    events.append(event)

                path = f"/artifacts/{token}"
                await server({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                              "scheme": "http", "method": "GET", "path": path, "raw_path": path.encode(),
                              "root_path": "", "query_string": b"", "headers": [],
                              "client": ("127.0.0.1", 12345), "server": ("localhost", 8088)}, receive, send)
                self.assertEqual(events[0]["type"], "http.response.start")
                self.assertEqual(events[0]["status"], 200)
                self.assertIn("水与冰".encode(), b"".join(event.get("body", b"") for event in events))
                self.assertEqual(len(client.roles), 6)  # Fetching an artifact never runs a model.
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
