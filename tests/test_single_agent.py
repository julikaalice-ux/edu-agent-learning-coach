"""Single-agent baseline: same program checks, one model with tools."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.db.memory import LearningStore
from learning_coach.domain.contracts import validate
from learning_coach.service.pipeline import NOTES_OFFER
from learning_coach.service.single_agent import SingleAgentPipeline, single_turn_schema
from test_pipeline import FakeArtifacts, QUIZ, RESPONSE_SCHEMA, VISUAL, exchange, messages

SDK_TOOLS = (importlib.util.find_spec("agent_framework") is not None
             and importlib.util.find_spec("agent_framework._tools") is not None)


def reply(message="先看整体被平均分成了几份。", primary="A", step=None):
    return {"schema_version": "1.0", "intent": {"primary": primary, "is_implicit": False},
            "message": message, "next_step": step}


def final(response=None, *, summary="学习分数", end=False, pause=False, stop_quiz=False, observe=False):
    return {"response": response or reply(), "task": {"task_summary": summary, "pause_task": pause, "end_task": end},
            "stop_quiz": stop_quiz, "observe_learning": observe}


def question(text=QUIZ["question"], **changes):
    value = deepcopy(QUIZ)
    value["question"] = text
    value.update(changes)
    return value


class Provider:
    def __init__(self, items=()):
        self.items = list(items)
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return deepcopy(self.items)


SOURCES = [
    {"title": "水", "url": "https://one.example/water", "excerpt": "Water expands when it freezes.",
     "excerpt_kind": "page_extract"},
    {"title": "冰", "url": "https://two.test/ice", "excerpt": "Ice is less dense than liquid water.",
     "excerpt_kind": "page_extract"},
]


class ScriptedAgent:
    """Plays one script per call: tool steps, then the final JSON. Records what it saw."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls = []
        self.tool_results = []

    async def __call__(self, task, payload, schema, tools):
        self.calls.append({"task": task, "payload": deepcopy(payload), "tools": [tool.name for tool in tools]})
        by_name = {tool.name: tool for tool in tools}
        script = self.scripts.pop(0)
        for step in script[:-1]:
            name, kwargs = step
            if callable(kwargs):
                kwargs = kwargs(self)
            result = await by_name[name].func(**kwargs)
            self.tool_results.append((name, result))
        answer = script[-1]
        answer = answer(self) if callable(answer) else answer
        return validate(answer, schema)


class ScriptedStreamAgent(ScriptedAgent):
    """Like ScriptedAgent, but stream_call emits the reply text in pieces first."""

    async def stream_call(self, task, payload, schema, tools, *, on_delta):
        self.calls.append({"task": task, "payload": deepcopy(payload), "tools": [tool.name for tool in tools]})
        by_name = {tool.name: tool for tool in tools}
        script = self.scripts.pop(0)
        for step in script[:-1]:
            name, kwargs = step
            if callable(kwargs):
                kwargs = kwargs(self)
            result = await by_name[name].func(**kwargs)
            self.tool_results.append((name, result))
        answer = script[-1]
        answer = answer(self) if callable(answer) else answer
        message = answer["response"]["message"]
        for start, end in ((0, 3), (3, 9), (9, len(message))):
            piece = message[start:end]
            if piece:
                on_delta(piece)
        return validate(answer, schema)


class SingleAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")
        self.artifacts = FakeArtifacts()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, agent, provider=None, **kwargs):
        return SingleAgentPipeline(agent, provider or Provider(), self.store, self.artifacts, RESPONSE_SCHEMA, **kwargs)

    async def test_direct_answer_uses_one_agent_call_and_no_tools(self):
        agent = ScriptedAgent([final()])
        output, state = await self.pipeline(agent).run(messages("分数是什么"), session_id="s")
        validate(output, RESPONSE_SCHEMA)
        self.assertEqual([call["task"] for call in agent.calls], ["teach"])
        self.assertEqual(agent.calls[0]["tools"], ["search", "record_claims", "draw_visual", "ask_question"])
        self.assertEqual(state["last_execution"], {"mode": "single", "tool_calls": [], "research": "not_requested",
                                                   "visual": "not_requested", "question": False})
        self.assertEqual(state["active_task"], "学习分数")

    async def test_research_is_checked_by_the_program_and_linked_by_it(self):
        claims = [{"text": "水结冰会膨胀", "status": "verified", "independent": True, "explanation": "两处节选",
                   "evidence": []}]

        def claims_from_sources(agent):
            sources = agent.tool_results[-1][1]["sources"]
            items = deepcopy(claims)
            items[0]["evidence"] = [{"source_id": item["id"], "quote": item["excerpt"], "relation": "supports"}
                                    for item in sources]
            return {"claims": items}

        provider = Provider(SOURCES)
        agent = ScriptedAgent([("search", {"question": "水结冰为什么膨胀", "queries": ["水 结冰 膨胀", "冰 密度"]}),
                               ("record_claims", claims_from_sources), final()])
        output, state = await self.pipeline(agent, provider).run(
            messages("查一下水结冰为什么膨胀"), session_id="s", external_permissions={"research"})
        self.assertEqual(provider.queries, ["水 结冰 膨胀", "冰 密度"])
        self.assertEqual(state["last_execution"]["research"], "verified")
        self.assertIn("参考资料：", output["message"])
        self.assertIn("https://one.example/water", output["message"])

    async def test_search_without_checking_claims_is_marked_unverified(self):
        agent = ScriptedAgent([("search", {"question": "量子纠缠", "queries": ["量子纠缠"]}), final()])
        output, state = await self.pipeline(agent, Provider(SOURCES)).run(
            messages("讲讲量子纠缠"), session_id="s", external_permissions={"research"})
        self.assertEqual(state["last_execution"]["research"], "unavailable")
        self.assertIn("本次未取得可核查资料", output["message"])

    async def test_a_url_the_program_did_not_retrieve_fails_the_turn(self):
        agent = ScriptedAgent([final(reply("看这里 https://made-up.example/page"))])
        output, state = await self.pipeline(agent).run(messages("分数是什么"), session_id="s")
        self.assertIn("这次处理没有顺利完成", output["message"])
        self.assertNotIn("last_execution", state)

    async def test_question_is_held_by_the_program_and_graded_next_turn(self):
        ask = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单", "total": 2,
                                "question": question()})
        second = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单", "total": 2,
                                   "question": question("四颗糖平均分给四个人，一人分到全部糖的几分之几？")})
        agent = ScriptedAgent([ask, final(reply("来做一题。", "C"))],
                              [second, final(reply("对了，下一题。", "C"))],
                              [final(reply("两题都对了。", "C"))])
        pipeline = self.pipeline(agent)
        self.store.set_enabled("s", True)
        output, state = await pipeline.run(messages("考考我分数"), session_id="s", turn_id="1")
        self.assertIn(QUIZ["question"], output["next_step"]["text"])
        self.assertNotIn("ANSWER_KEY_PRIVATE", json.dumps(output, ensure_ascii=False))
        self.assertEqual(state["quiz"]["awaiting"]["answer_index"], 0)

        history = exchange("考考我分数", reply="来做一题。")
        output, state = await pipeline.run(messages("A", *history), session_id="s", state=state, turn_id="2")
        seen = agent.calls[1]["payload"]
        self.assertTrue(seen["quiz_feedback"]["feedback"]["correct"])
        self.assertEqual(seen["quiz_feedback"]["next_question_needed"]["number"], 2)
        self.assertNotIn("answer_index", json.dumps(seen["state"], ensure_ascii=False))
        self.assertNotIn("ANSWER_KEY_PRIVATE", json.dumps(seen["state"], ensure_ascii=False))
        self.assertIn("四颗糖", output["next_step"]["text"])

        history += ({"role": "user", "text": "A"}, {"role": "assistant", "text": "对了，下一题。"})
        output, state = await pipeline.run(messages("A", *history), session_id="s", state=state, turn_id="3")
        self.assertTrue(agent.calls[2]["payload"]["quiz_feedback"]["finished"])
        self.assertEqual(output["next_step"]["text"], NOTES_OFFER)
        self.assertTrue(state["notes_offer"])
        self.assertEqual(state["context"]["quiz_results"][-1]["correct"], 2)

    async def test_wrong_answer_passes_the_suspected_error(self):
        ask = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单", "total": 3,
                                "question": question()})
        agent = ScriptedAgent([ask, final(reply("来做一题。", "C"))], [final(reply("你当时是怎么想的？", "C"))])
        pipeline = self.pipeline(agent)
        _, state = await pipeline.run(messages("考考我分数"), session_id="s", turn_id="1")
        output, state = await pipeline.run(messages("B", *exchange("考考我分数")), session_id="s", state=state,
                                           turn_id="2")
        feedback = agent.calls[1]["payload"]["quiz_feedback"]["feedback"]
        self.assertFalse(feedback["correct"])
        self.assertEqual(feedback["suspected_error"]["code"], "misreading")
        self.assertIsNone(state["quiz"]["awaiting"])

    async def test_question_checks_match_the_quiz_writer(self):
        bad = question(diagnoses=[])
        bad["diagnoses"] = [{"choice_index": 0, "error_type": "conceptual", "misconception": "标在了正确选项上"}]
        attempt = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单", "total": 3,
                                    "question": bad})
        agent = ScriptedAgent([attempt, attempt, attempt, final(reply("先讲一讲。"))])
        output, state = await self.pipeline(agent).run(messages("考考我分数"), session_id="s")
        problems = [result["problem"] for name, result in agent.tool_results]
        self.assertEqual(problems, ["every_wrong_choice_needs_one_diagnosis"] * 2 + ["question_limit"])
        self.assertNotIn("quiz", state)
        self.assertFalse(state["last_execution"]["question"])

    async def test_one_question_per_turn_and_no_check_inside_a_practice_set(self):
        ask = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单", "total": 3,
                                "question": question()})
        check = ("ask_question", {"kind": "check", "topic": "分数", "difficulty": "简单", "total": 1,
                                  "question": question("四颗糖平均分给四个人，一人分到全部糖的几分之几？")})
        agent = ScriptedAgent([ask, check, final(reply("来做一题。", "C"))])
        _, state = await self.pipeline(agent).run(messages("考考我分数"), session_id="s")
        self.assertEqual(agent.tool_results[1][1]["problem"], "one_question_per_turn")
        self.assertEqual(state["quiz"]["kind"], "practice")

    async def test_check_question_after_an_explanation(self):
        check = ("ask_question", {"kind": "check", "topic": "分数的意义", "difficulty": "简单", "total": 3,
                                  "question": question()})
        agent = ScriptedAgent([check, final(reply("分数表示整体被平均分后取其中几份。"))])
        output, state = await self.pipeline(agent).run(messages("分数是什么"), session_id="s")
        self.assertEqual((state["quiz"]["kind"], state["quiz"]["total"]), ("check", 1))
        self.assertEqual(output["next_step"]["kind"], "question")

    async def test_visual_is_rendered_checked_and_committed_after_the_reply(self):
        broken = {"kind": "comparison", "title": "缺字段"}
        agent = ScriptedAgent([("draw_visual", {"goal": "对照分子分母", "spec": broken}),
                               ("draw_visual", {"goal": "对照分子分母", "spec": VISUAL}), final()])
        output, state = await self.pipeline(agent).run(messages("画个图对比分子分母"), session_id="s")
        self.assertEqual(agent.tool_results[0][1]["problem"], "schema_invalid")
        self.assertTrue(agent.tool_results[1][1]["ok"])
        self.assertIn("[打开互动图]", output["message"])
        self.assertEqual(len(self.artifacts.committed), 1)
        self.assertEqual(state["last_execution"]["visual"], "published")

    async def test_failed_visual_is_reported_and_nothing_is_published(self):
        broken = {"kind": "comparison", "title": "缺字段"}
        agent = ScriptedAgent([("draw_visual", {"goal": "对照", "spec": broken}),
                               ("draw_visual", {"goal": "对照", "spec": broken}),
                               ("draw_visual", {"goal": "对照", "spec": VISUAL}), final()])
        output, state = await self.pipeline(agent).run(messages("画个图"), session_id="s")
        self.assertEqual(agent.tool_results[2][1]["problem"], "draft_limit")
        self.assertIn("这次未生成可用的互动图", output["message"])
        self.assertEqual(self.artifacts.committed, [])

    async def test_stop_quiz_clears_the_set_and_the_next_step(self):
        ask = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单", "total": 3,
                                "question": question()})
        agent = ScriptedAgent([ask, final(reply("来做一题。", "C"))],
                              [final(reply("好，先停在这里。", "C"), stop_quiz=True)])
        pipeline = self.pipeline(agent)
        _, state = await pipeline.run(messages("考考我分数"), session_id="s", turn_id="1")
        output, state = await pipeline.run(messages("先到这里，不继续了", *exchange("考考我分数")), session_id="s",
                                           state=state, turn_id="2")
        self.assertIsNone(output["next_step"])
        self.assertNotIn("quiz", state)

    async def test_skip_and_notes_go_to_the_same_agent(self):
        ask = ("ask_question", {"kind": "practice", "topic": "分数", "difficulty": "简单", "total": 3,
                                "question": question()})
        replacement = question("四颗糖平均分给四个人，一人分到全部糖的几分之几？")
        notes = {"schema_version": "1.0", "profile": [], "error_records": [], "review_plan": [],
                 "concepts": [{"concept_id": "fractions", "label": "分数", "prerequisites": None, "related": None,
                               "evidence_quote": "考考我分数"}]}
        agent = ScriptedAgent([ask, final(reply("来做一题。", "C"))], [replacement], [notes])
        pipeline = self.pipeline(agent)
        self.store.set_enabled("s", True)
        _, state = await pipeline.run(messages("考考我分数"), session_id="s", turn_id="1")
        output, state = await pipeline.run(messages("换一题", *exchange("考考我分数")), session_id="s",
                                           state=state, turn_id="2")
        self.assertIn("四颗糖", output["next_step"]["text"])
        history = exchange("考考我分数", "换一题")
        output, state = await pipeline.run(messages("整理笔记", *history), session_id="s", state=state, turn_id="3")
        self.assertEqual([call["task"] for call in agent.calls], ["teach", "write_question", "compile_notes"])
        self.assertEqual(agent.calls[1]["tools"], [])
        self.assertIn("1 个知识点", output["message"])

    async def test_onboarding_and_record_commands_are_program_steps(self):
        agent = ScriptedAgent()
        pipeline = self.pipeline(agent, onboarding=True)
        output, _ = await pipeline.run(messages("你好"), session_id="s")
        self.assertIn("这次是谁在学", output["next_step"]["text"])
        output, _ = await pipeline.run(messages("/memory on"), session_id="t")
        self.assertEqual(agent.calls, [])

    def test_final_schema_wraps_the_public_reply(self):
        schema = single_turn_schema(RESPONSE_SCHEMA)
        self.assertEqual(set(schema["required"]), {"response", "task", "stop_quiz", "observe_learning"})
        validate(final(), schema)


class SingleAgentPromptTests(unittest.TestCase):
    def test_prompt_is_assembled_from_the_shared_rule_files(self):
        from learning_coach.adapters.single_runner import compose_instructions
        text = compose_instructions(SOURCE / "prompts")
        quiz_rules = (SOURCE / "prompts/agents/quiz-writer.md").read_text(encoding="utf-8")
        coordinator = (SOURCE / "prompts/instructions.md").read_text(encoding="utf-8")
        for snippet in ("每个错误选项都必须是", "什么时候请求配图"):
            self.assertIn(snippet, quiz_rules)
            self.assertIn(snippet, text)
        self.assertIn("## 按意图执行（跨轮进行）", text)
        self.assertIn("search_queries 写 2–4 条短查询", coordinator)
        self.assertIn("search_queries 写 2–4 条短查询", text)
        # The multi-agent orchestration protocol is not part of the single agent's prompt.
        self.assertNotIn("# 编排阶段", text)
        self.assertNotIn("# 对话上下文", text)


@unittest.skipUnless(SDK_TOOLS, "needs the Agent Framework SDK")
class SingleAgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    def client(self, script):
        from agent_framework import BaseChatClient, ChatResponse, Content, Message
        from agent_framework._middleware import ChatMiddlewareLayer
        from agent_framework._tools import FunctionInvocationLayer
        from agent_framework.observability import ChatTelemetryLayer

        class Scripted(FunctionInvocationLayer, ChatMiddlewareLayer, ChatTelemetryLayer, BaseChatClient):
            def __init__(self):
                super().__init__()
                self.script, self.requests = list(script), []

            async def _inner_get_response(self, *, messages, stream, options, **kwargs):
                self.requests.append({"roles": [m.role for m in messages],
                                      "tool_choice": (options or {}).get("tool_choice")})
                kind, value = self.script.pop(0)
                if kind == "call":
                    contents = [Content.from_function_call(call_id=f"c{len(self.requests)}", name=value[0],
                                                           arguments=json.dumps(value[1], ensure_ascii=False))]
                else:
                    contents = [Content.from_text(value)]
                response = ChatResponse(messages=[Message("assistant", contents)])
                response.usage_details = {"input_token_count": 100, "output_token_count": 10}
                return response

        return Scripted()

    async def test_tools_run_in_the_loop_and_a_bad_final_is_fixed_without_rerunning_them(self):
        from learning_coach.adapters.single_runner import SingleAgentRunner, create_single_agent
        from learning_coach.service.ports import ToolSpec

        ran = []

        async def lookup(question):
            ran.append(question)
            return {"ok": True, "answer": 42}

        tool = ToolSpec("lookup", "查一下", {"type": "object", "properties": {"question": {"type": "string"}},
                                           "required": ["question"]}, lookup)
        client = self.client([("call", ("lookup", {"question": "q"})), ("text", "not json"),
                              ("text", json.dumps({"ok": True}))])
        records = []
        runner = SingleAgentRunner(create_single_agent(client, SOURCE / "prompts"), observer=records.append)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                  "additionalProperties": False}
        self.assertEqual(await runner("teach", {"x": 1}, schema, [tool]), {"ok": True})
        self.assertEqual(ran, ["q"])
        self.assertEqual(client.requests[-1]["tool_choice"], "none")
        record = records[-1]
        self.assertEqual((record["model_calls"], record["tool_calls"], record["attempt"], record["outcome"]),
                         (3, 1, 2, "ok"))
        self.assertEqual(record["input_token_count"], 300)


class ServerWiringTests(unittest.TestCase):
    def test_architecture_switch_builds_the_single_agent_pipeline(self):
        if importlib.util.find_spec("agent_framework.foundry") is None:
            self.skipTest("needs the Agent Framework SDK")
        from unittest.mock import patch
        from learning_coach.server import architecture_from_env, create_pipeline
        with tempfile.TemporaryDirectory() as folder, \
                patch("learning_coach.server.DefaultAzureCredential", side_effect=AssertionError("credentials")):
            pipeline = create_pipeline(client=object(), data_dir=folder, architecture="single",
                                       search_provider=Provider(), image_generator=lambda prompt: b"")
            self.assertIsInstance(pipeline, SingleAgentPipeline)
            pipeline.store.close()
        with patch.dict("os.environ", {"LEARNING_COACH_ARCHITECTURE": "single"}):
            self.assertEqual(architecture_from_env(), "single")
        with patch.dict("os.environ", {"LEARNING_COACH_ARCHITECTURE": "both"}):
            with self.assertRaises(ValueError):
                architecture_from_env()


class MessageExtractionTests(unittest.TestCase):
    """Incremental JSON message-field extraction for streaming."""

    def test_plain_text_splits_and_escapes(self):
        from learning_coach.adapters.single_runner import extract_message_partial
        self.assertEqual(extract_message_partial('{"message":"你好', 0), "你好")
        self.assertEqual(extract_message_partial('{"message":"a\\nb\\"c', 0), "a\nb\"c")
        self.assertEqual(extract_message_partial('{"message":"abc","next_step":null}', 0), "abc")
        self.assertEqual(extract_message_partial('{"message":"abc","next_step":null}', 3), "")
        self.assertEqual(extract_message_partial('{"message":"abcd\\', 0), "abcd")  # split escape waits
        self.assertEqual(extract_message_partial('{"tool_calls":[{"name":"search"}]}', 0), "")
        self.assertEqual(extract_message_partial('{"message":""}', 0), "")

    def test_nested_message_key_inside_tool_round_is_not_the_reply(self):
        from learning_coach.adapters.single_runner import extract_message_partial
        # The raw pattern only matches a real JSON key, not escaped argument text.
        payload = '{"response":{"message":"hello","next_step":null}}'
        self.assertEqual(extract_message_partial(payload, 0), "hello")


class StreamingPipelineTests(SingleAgentTests):
    async def test_run_stream_yields_deltas_then_the_validated_reply(self):
        agent = ScriptedStreamAgent([final(reply("分数表示整体被平均分成的份数。"))])
        events = [event async for event in self.pipeline(agent).run_stream(messages("分数是什么"), session_id="s",
                                                                           returning_visitor=True)]
        deltas = "".join(event["delta"] for event in events if "delta" in event)
        last = [event for event in events if "output" in event][-1]
        self.assertEqual(deltas, "分数表示整体被平均分成的份数。")
        self.assertEqual(last["output"]["message"], "分数表示整体被平均分成的份数。")
        self.assertEqual(last["state"]["last_execution"]["mode"], "single")

    async def test_run_stream_matches_plain_run_results(self):
        text = "分数表示整体被平均分成的份数。"
        streamed = [event async for event in self.pipeline(ScriptedStreamAgent([final(reply(text))]))
                    .run_stream(messages("分数是什么"), session_id="s1")]
        plain_output, plain_state = await self.pipeline(ScriptedAgent([final(reply(text))]))             .run(messages("分数是什么"), session_id="s2")
        final_event = [event for event in streamed if "output" in event][-1]
        self.assertEqual(final_event["output"], plain_output)
        self.assertEqual(final_event["state"]["active_task"], plain_state["active_task"])

    async def test_run_stream_after_a_failed_draft_yields_the_fallback_reply(self):
        from learning_coach.service.execution import StageFailure

        class FailingStreamAgent(ScriptedStreamAgent):
            async def stream_call(self, task, payload, schema, tools, *, on_delta):
                on_delta("看到一半的草稿")
                raise StageFailure("invalid_stage_output")

        events = [event async for event in self.pipeline(FailingStreamAgent([final()]))
                  .run_stream(messages("分数是什么"), session_id="s")]
        last = [event for event in events if "output" in event][-1]
        self.assertIn("这次处理没有顺利完成", last["output"]["message"])


if __name__ == "__main__":
    unittest.main()
