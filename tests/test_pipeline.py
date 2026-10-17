"""Offline orchestration tests with injected agents, search and artifact storage."""

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(SOURCE))

from learning_coach.domain.contracts import validate
from learning_coach.db.memory import LearningStore, NOTES_SCHEMA
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.adapters.runner import StageFailure
from learning_coach.service.ports import StorageError
from learning_coach.adapters.visuals import VISUAL_SCHEMA


RESPONSE_SCHEMA = json.loads((SOURCE / "response.schema.json").read_text(encoding="utf-8"))
QUIZ = {
    "concept_id": "fractions", "question": "四块饼分出一块，分出了整体的多少？",
    "choices": ["四分之一", "四分之三"], "answer_index": 0,
    "explanation": "ANSWER_KEY_PRIVATE: 一块占四块中的一块。",
    "diagnoses": [{"choice_index": 1, "error_type": "misreading",
                   "misconception": "PRIVATE_DIAGNOSIS: 把“分出的”看成了“剩下的”"}],
    "image": None,
}
QUIZ_VARIANTS = (
    QUIZ["question"],
    "四颗糖平均分给四个人，一人分到全部糖的几分之几？",
    "四張卡片涂色一张，涂色部分占整体的多少？",
)
VISUAL = {
    "kind": "comparison", "title": "分数对照", "left_label": "分子", "right_label": "分母",
    "rows": [{"aspect": "表示", "left": "取出的份数", "right": "平均分成的总份数"}],
}


def plan(primary="A", **updates):
    value = {
        "intent": {"primary": primary, "is_implicit": False},
        "research_query": None, "search_queries": None, "visual_request": None, "observe_learning": False,
        "task_summary": "学习分数", "pause_task": False, "end_task": False,
        "quiz_action": "none", "quiz_topic": None, "quiz_total": 3,
        "difficulty": "简单", "participant": "learner",
        "visual_uses_research": None, "quiz_uses_research": None, "quiz_uses_visual": None,
    }
    value.update(updates)
    if value["research_query"] and "search_queries" not in updates:
        # The coordinator writes the search queries along with the research question.
        value["search_queries"] = [value["research_query"][:200]]
    return value


def notes(with_profile=False):
    value = {"schema_version": "1.0", "profile": [], "concepts": [], "error_records": [], "review_plan": []}
    if with_profile:
        value["profile"] = [{"key": "learning_stage", "value": "职场", "origin": "self_report", "evidence_quote": "我在工作"}]
    validate(value, NOTES_SCHEMA)
    return value


def messages(text, *history):
    return [*history, {"role": "user", "text": text}]


def exchange(*texts, reply="我们先看整体被分成了几份。"):
    """Earlier turns: each learner text followed by a coach reply."""
    return tuple(item for text in texts for item in ({"role": "user", "text": text}, {"role": "assistant", "text": reply}))


class FakeRunner:
    """Only implements the documented role/phase contract; does no I/O."""

    def __init__(self, plans=(), *, replies=(), quiz=(), note_results=(), visuals=(), visual_reviews=()):
        self.plans = deque(plans)
        self.replies = deque(replies)
        self.questions = deque(quiz)
        self.note_results = deque(note_results)
        self.visuals = deque(visuals)
        self.visual_reviews = deque(visual_reviews)
        self.calls = []

    async def __call__(self, role, payload, schema):
        self.calls.append({"role": role, "payload": deepcopy(payload), "schema": deepcopy(schema)})
        if role == "coordinator" and payload["phase"] == "decide":
            proposed = self.plans.popleft() if self.plans else plan()
            if isinstance(proposed, Exception):
                raise proposed
            if "mode" in proposed:
                result = proposed
            else:
                halted = proposed["intent"]["primary"] in "EFG" or proposed["end_task"] or proposed["pause_task"] or proposed["quiz_action"] == "stop"
                selected = bool(proposed["research_query"] or proposed["visual_request"] or proposed["observe_learning"]
                                or (proposed["quiz_action"] in ("start", "continue") and proposed["intent"]["primary"] in "ABC"))
                if halted or not selected:
                    response = self.replies.popleft() if self.replies else self.default_reply(proposed["intent"])
                    if isinstance(response, Exception):
                        raise response
                    result = {"mode": "answer", "response": response,
                              "task": {"task_summary": proposed["task_summary"], "pause_task": proposed["pause_task"],
                                       "end_task": proposed["end_task"] or proposed["quiz_action"] == "stop"}}
                else:
                    current_text = payload["messages"][-1]["text"]
                    proposed = deepcopy(proposed)
                    proposed["tool_reasons"] = {
                        "research": {"code": "knowledge_gap", "evidence_quote": current_text} if proposed["research_query"] else None,
                        "quiz": {"code": "continue_practice" if proposed["quiz_action"] == "continue" else "explicit_practice",
                                 "evidence_quote": current_text} if proposed["quiz_action"] in ("start", "continue") else None,
                        "notes": {"code": "profile_update", "evidence_quote": current_text} if proposed["observe_learning"] else None,
                        "visual": {"code": "comparison", "evidence_quote": current_text} if proposed["visual_request"] else None,
                    }
                    result = {"mode": "tools", "plan": proposed}
            # Decisions carry the coordinator's updated conversation notes; null = no change.
            result = {**result, "context_notes": result.get("context_notes"),
                      "check_question": result.get("check_question")}
        elif role == "quizzer":
            if self.questions:
                result = self.questions.popleft()
            else:
                number = len(self.calls_for("quizzer")) - 1
                result = {**QUIZ, "question": QUIZ_VARIANTS[number] if number < len(QUIZ_VARIANTS) else f"练习 {number + 1}：{QUIZ['question']}"}
        elif role == "coordinator" and payload["phase"] == "respond":
            result = self.replies.popleft() if self.replies else self.default_reply(payload["plan"]["intent"])
        elif role == "notes":
            result = self.note_results.popleft() if self.note_results else notes()
        elif role == "visualizer":
            result = self.visuals.popleft() if self.visuals else VISUAL
        elif role == "verifier" and payload.get("phase") == "visual_review":
            result = self.visual_reviews.popleft() if self.visual_reviews else {
                "verdict": "pass", "checks": {key: True for key in (
                    "request_match", "factual_consistency", "labels_clear", "interaction_useful", "not_misleading")},
                "issues": [], "fact_basis": "general_knowledge",
            }
        else:
            raise AssertionError(f"Unexpected role or phase: {role}")
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    @staticmethod
    def default_reply(intent):
        return {"schema_version": "1.0", "intent": deepcopy(intent),
                "message": "我们先看整体被分成了几份。",
                "next_step": {"kind": "question", "text": "你愿意从哪一步开始？"}}

    def calls_for(self, role, phase=None):
        return [call["payload"] for call in self.calls
                if call["role"] == role and (phase is None or call["payload"].get("phase") == phase)]


class FakeResearch:
    def __init__(self, result=None):
        self.result = result or {"overall": "verified", "sources": [], "claims": []}
        self.calls = []

    async def run(self, query, queries=None):
        self.calls.append(query)
        self.queries = getattr(self, "queries", []) + [queries]
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)


class FakeArtifacts:
    def __init__(self):
        self.prepared, self.committed, self.deleted = [], [], []

    def prepare(self, session_id, spec, image=None):
        validate(spec, VISUAL_SCHEMA)
        self.prepared.append((session_id, deepcopy(spec)))
        return {"url": "/learning-artifacts/fake-local-token", "spec": deepcopy(spec)}

    def commit(self, session_id, artifact):
        self.committed.append((session_id, deepcopy(artifact)))

    def delete_session(self, session_id):
        self.deleted.append(session_id)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")
        self.research = FakeResearch()
        self.artifacts = FakeArtifacts()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, research=None):
        return LearningPipeline(runner, research or self.research, self.store, self.artifacts, RESPONSE_SCHEMA)

    def assert_protocol(self, output):
        validate(output, RESPONSE_SCHEMA)
        self.assertEqual(set(output), {"schema_version", "intent", "message", "next_step"})

    async def test_first_meeting_flag_reaches_coordinator(self):
        runner = FakeRunner([plan(research_query="分数定义")])
        await self.pipeline(runner).run(messages("你好，想学分数"), session_id="new", turn_id="1")
        decide = runner.calls_for("coordinator", "decide")[0]
        self.assertIs(decide["first_meeting"], True)
        self.assertIs(runner.calls_for("coordinator", "respond")[0]["first_meeting"], True)
        later = FakeRunner([plan()])
        history = ({"role": "user", "text": "你好"}, {"role": "assistant", "text": "你好，我是学习教练。"})
        await self.pipeline(later).run(messages("讲讲分数", *history), session_id="new2", turn_id="1")
        self.assertIs(later.calls_for("coordinator", "decide")[0]["first_meeting"], False)

    async def test_returning_visitor_is_not_greeted_again(self):
        runner = FakeRunner([plan()])
        await self.pipeline(runner).run(messages("你好"), session_id="c2", returning_visitor=True)
        self.assertIs(runner.calls_for("coordinator", "decide")[0]["first_meeting"], False)

    async def test_learner_records_carry_across_conversations(self):
        self.store.set_enabled("learner-1", True)
        runner = FakeRunner([plan(observe_learning=True)], note_results=[notes(True)])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("我在工作，想学习分数"), session_id="c1",
                                      turn_id="1", learner_id="learner-1")
        await pipeline.run(messages("整理笔记", *exchange("我在工作，想学习分数")), session_id="c1",
                           state=state, turn_id="2", learner_id="learner-1")
        later = FakeRunner([plan()])
        await self.pipeline(later).run(messages("接着上次的"), session_id="c2", turn_id="1",
                                       learner_id="learner-1")
        decide = later.calls_for("coordinator", "decide")[0]
        self.assertIn("learning_stage", decide["memory"]["profile"])
        self.assertIs(decide["first_meeting"], False)
        # Without the learner ID, a new conversation does not see those records.
        other = FakeRunner([plan()])
        await self.pipeline(other).run(messages("你好"), session_id="c3", turn_id="1")
        self.assertEqual(other.calls_for("coordinator", "decide")[0]["memory"]["profile"], {})

    async def test_routes_learning_through_notes_research_and_visual_then_commits(self):
        self.store.set_enabled("s", True)
        runner = FakeRunner([plan(research_query="分数定义", visual_request="比较分子分母", observe_learning=True)], note_results=[notes(True)])
        pipeline = self.pipeline(runner)
        output, state = await pipeline.run(messages("我在工作，想学习分数"), session_id="s", turn_id="1")
        self.assert_protocol(output)
        self.assertEqual(self.research.calls, ["分数定义"])
        self.assertEqual([runner.calls[0]["role"], runner.calls[-1]["role"]], ["coordinator", "coordinator"])
        self.assertCountEqual([call["role"] for call in runner.calls[1:-1]], ["visualizer", "verifier"])
        self.assertEqual(len(self.artifacts.committed), 1)
        # The learning evidence is marked; notes wait until the learner agrees.
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        self.assertEqual(state["context"]["evidence_turns"], [1])
        self.assertEqual(state["revision"], 1)
        self.assertIn("打开互动图", output["message"])
        compiled, state = await pipeline.run(messages("整理笔记", *exchange("我在工作，想学习分数")),
                                             session_id="s", state=state, turn_id="2")
        self.assert_protocol(compiled)
        self.assertIn("1 条学习情况", compiled["message"])
        request = runner.calls_for("notes")[0]
        self.assertEqual(request["learner_messages"], [{"turn": 1, "text": "我在工作，想学习分数"}])
        self.assertEqual(request["evidence_turns"], [1])
        self.assertIn("learning_stage", self.store.snapshot("s")["profile"])
        self.assertEqual((state["context"]["notes_upto_turn"], state["context"]["evidence_turns"]), (2, []))

    async def test_foundry_mirror_writes_facts_and_survives_search_failure(self):
        from learning_coach.adapters.foundry_memory import FoundryMemoryStore

        class FakeFoundryClient:
            def __init__(self):
                self.created = []

            def create_memory(self, name, *, scope, content, kind):
                self.created.append((name, scope, content, kind))

            def search_memories(self, name, *, scope, items=None, options=None):
                return SimpleNamespace(memories=[])

            def delete_scope(self, name, scope):
                pass

            def close(self):
                pass

        class OfflineFoundryClient(FakeFoundryClient):
            def search_memories(self, *args, **kwargs):
                raise TimeoutError("memory store unreachable")

        client = FakeFoundryClient()
        wrapped = FoundryMemoryStore(self.store, client, "m")
        wrapped.set_enabled("s", True)
        runner = FakeRunner([plan(observe_learning=True)], note_results=[notes(True)])
        pipeline = LearningPipeline(runner, self.research, wrapped, self.artifacts, RESPONSE_SCHEMA)
        _, state = await pipeline.run(messages("我在工作，想学习分数"), session_id="s", turn_id="1")
        output, state = await pipeline.run(messages("整理笔记", *exchange("我在工作，想学习分数")),
                                           session_id="s", state=state, turn_id="2")
        self.assert_protocol(output)
        # The validated notes were mirrored under the memory key (the session here).
        self.assertEqual(client.created, [("m", "s", "档案: 学习阶段=职场（自述）", "user_profile")])
        # A search outage must not break the next turn's reply or its memory view.
        offline = FoundryMemoryStore(self.store, OfflineFoundryClient(), "m")
        later = FakeRunner([plan()])
        offline_pipeline = LearningPipeline(later, self.research, offline, self.artifacts, RESPONSE_SCHEMA)
        result, _ = await offline_pipeline.run(messages("接着讲讲", *exchange("我在工作，想学习分数")),
                                               session_id="s", state=state, turn_id="3")
        self.assert_protocol(result)
        self.assertEqual(later.calls_for("coordinator", "decide")[0]["memory"]["semantic"], [])

    async def test_emotion_boundary_and_unclear_intents_never_run_learning_workers(self):
        self.store.set_enabled("s", True)
        for index, primary in enumerate("EFG"):
            with self.subTest(primary=primary):
                runner = FakeRunner([plan(primary, research_query="不应检索", visual_request="不应绘图", observe_learning=True, quiz_action="start", quiz_topic="分数")])
                output, _ = await self.pipeline(runner).run(messages("这一轮"), session_id="s", turn_id=str(index))
                self.assert_protocol(output)
                self.assertEqual(output["intent"]["primary"], primary)
                self.assertEqual([call["role"] for call in runner.calls], ["coordinator"])
                self.assertEqual(runner.calls_for("quizzer"), [])
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.artifacts.prepared, [])

    async def test_default_memory_off_does_not_call_notes_or_write_profile(self):
        runner = FakeRunner([plan(observe_learning=True)], note_results=[notes(True)])
        output, _ = await self.pipeline(runner).run(messages("我在工作"), session_id="s", turn_id="1")
        self.assert_protocol(output)
        self.assertEqual(runner.calls_for("notes"), [])
        self.assertFalse(self.store.is_enabled("s"))
        self.assertEqual(self.store.snapshot("s")["profile"], {})

    async def test_memory_commands_bypass_agents_and_forget_clears_state_and_artifacts(self):
        runner = FakeRunner([plan(observe_learning=True)], note_results=[notes(True)])
        pipeline = self.pipeline(runner)
        enabled, state = await pipeline.run(messages("/memory on"), session_id="s", turn_id="on")
        self.assert_protocol(enabled)
        self.assertTrue(self.store.is_enabled("s"))
        self.assertEqual(runner.calls, [])
        _, state = await pipeline.run(messages("我在工作"), session_id="s", state=state, turn_id="learn")
        _, state = await pipeline.run(messages("整理笔记", *exchange("我在工作")), session_id="s", state=state, turn_id="notes")
        self.assertIn("learning_stage", self.store.snapshot("s")["profile"])
        _, state = await pipeline.run(messages("/memory off"), session_id="s", state=state, turn_id="off")
        self.assertFalse(self.store.is_enabled("s"))
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        forgotten, state = await pipeline.run(messages("/forget"), session_id="s", state=state, turn_id="forget")
        self.assert_protocol(forgotten)
        self.assertEqual(self.artifacts.deleted, ["s"])
        self.assertFalse(self.store.is_enabled("s"))
        self.assertNotIn("active_task", state)
        self.assertNotIn("quiz", state)
        self.store.set_enabled("s", True)
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        self.assertEqual(len(runner.calls), 3)

    async def test_partial_forget_stops_memory_and_clears_tasks_until_explicit_reenable(self):
        self.store.set_enabled("s", True)
        self.store.apply_notes("s", "seed", notes(True), "我在工作")
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("A", observe_learning=True),
            plan("A", observe_learning=True),
        ], note_results=[notes(True)])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        with patch.object(self.artifacts, "delete_session", side_effect=OSError("PRIVATE_DELETE_ERROR")), self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            output, state = await pipeline.run(messages("/forget"), session_id="s", state=state, turn_id="forget")
        self.assert_protocol(output)
        self.assertIn("部分已存内容未能删除", output["message"])
        self.assertNotIn("已删除本会话保存的学习笔记和可视化文件", output["message"])
        self.assertNotIn("PRIVATE_DELETE_ERROR", output["message"])
        self.assertTrue(state["memory_blocked"])
        self.assertNotIn("quiz", state)
        self.assertNotIn("active_task", state)
        self.assertNotIn("pending_tasks", state)
        self.assertFalse(self.store.is_enabled("s"))
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        with patch.object(self.store, "is_enabled", side_effect=AssertionError("blocked memory must not query consent")) as check, patch.object(self.store, "snapshot", side_effect=AssertionError("blocked memory must not read")) as read, patch.object(self.store, "apply_notes", side_effect=AssertionError("blocked memory must not write")) as write:
            continued, state = await pipeline.run(messages("我在工作，继续学"), session_id="s", state=state, turn_id="blocked")
            self.assert_protocol(continued)
            check.assert_not_called()
            read.assert_not_called()
            write.assert_not_called()
        self.assertEqual(runner.calls_for("notes"), [])
        _, state = await pipeline.run(messages("/memory on"), session_id="s", state=state, turn_id="enable")
        self.assertFalse(state["memory_blocked"])
        self.assertTrue(self.store.is_enabled("s"))
        _, state = await pipeline.run(messages("我在工作"), session_id="s", state=state, turn_id="after-enable")
        await pipeline.run(messages("整理笔记", *exchange("我在工作")), session_id="s", state=state, turn_id="notes")
        self.assertEqual(len(runner.calls_for("notes")), 1)
        self.assertIn("learning_stage", self.store.snapshot("s")["profile"])

    async def test_sessions_do_not_share_memory_or_pending_quiz(self):
        self.store.set_enabled("first", True)
        self.store.apply_notes("first", "seed", notes(True), "我在工作")
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("A", task_summary="另一位学习者"),
        ])
        pipeline = self.pipeline(runner)
        _, state_a = await pipeline.run(messages("考考我"), session_id="first", turn_id="1")
        _, state_b = await pipeline.run(messages("学习新内容"), session_id="second", turn_id="1")
        self.assertIn("quiz", state_a)
        self.assertNotIn("quiz", state_b)
        self.assertEqual(runner.calls_for("coordinator", "decide")[1]["memory"]["profile"], {})
        self.assertEqual(runner.calls_for("coordinator", "decide")[1]["state"], {})

    async def test_state_cannot_be_reused_under_another_session_owner(self):
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数")])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="first", turn_id="1")
        count = len(runner.calls)
        with self.assertRaisesRegex(ValueError, "session_state_owner_mismatch"):
            await pipeline.run(messages("继续"), session_id="second", state=state, turn_id="2")
        self.assertEqual(len(runner.calls), count)

    async def test_forget_removes_prior_visible_history_from_future_agent_inputs(self):
        runner = FakeRunner()
        pipeline = self.pipeline(runner)
        history = [{"role": "user", "text": "PRIVATE_BEFORE_FORGET"}, {"role": "assistant", "text": "PRIVATE_OLD_REPLY"}]
        _, state = await pipeline.run(messages("/forget", *history), session_id="s", turn_id="forget")
        history.append({"role": "user", "text": "/forget"})
        await pipeline.run(messages("学习新内容", *history), session_id="s", state=state, turn_id="after")
        self.assertNotIn("PRIVATE_BEFORE_FORGET", json.dumps(runner.calls))
        self.assertNotIn("PRIVATE_OLD_REPLY", json.dumps(runner.calls))

    async def test_transport_replay_does_not_generate_question_or_notes_again(self):
        self.store.set_enabled("s", True)
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数", observe_learning=True)], note_results=[notes(True)])
        pipeline = self.pipeline(runner)
        request = messages("我在工作，考考我")
        first, state = await pipeline.run(request, session_id="s", turn_id="same")
        replay, replay_state = await pipeline.run(request, session_id="s", turn_id="same")
        self.assertEqual(first, replay)
        self.assertEqual(state, replay_state)
        self.assertEqual(len(runner.calls_for("quizzer")), 1)
        self.assertEqual(runner.calls_for("notes"), [])
        self.assertEqual(state["context"]["evidence_turns"], [1])

    async def test_delayed_transport_replay_does_not_rerun_an_older_accepted_turn(self):
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("C", quiz_action="continue", quiz_topic="分数"),
        ])
        pipeline = self.pipeline(runner)
        original = messages("考考我")
        first, state = await pipeline.run(original, session_id="s", turn_id="start")
        _, state = await pipeline.run(messages("B"), session_id="s", state=state, turn_id="answer")
        count = len(runner.calls)
        replay, after = await pipeline.run(original, session_id="s", turn_id="start")
        self.assertEqual(replay, first)
        self.assertEqual(len(runner.calls), count)
        self.assertEqual(after["quiz"]["answered"], state["quiz"]["answered"])

    async def test_reused_turn_id_with_different_input_is_rejected(self):
        runner = FakeRunner()
        pipeline = self.pipeline(runner)
        await pipeline.run(messages("第一个问题"), session_id="s", turn_id="same")
        with self.assertRaisesRegex(ValueError, "turn_id_conflict"):
            await pipeline.run(messages("不同的问题"), session_id="s", turn_id="same")

    async def test_unanswered_question_hides_answer_key_and_does_not_advance(self):
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("C", quiz_action="continue", quiz_topic="分数"),
        ])
        pipeline = self.pipeline(runner)
        output, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        self.assertEqual(output["next_step"]["text"].count(QUIZ["question"]), 1)
        self.assertEqual(runner.calls_for("quizzer")[0]["count"], 1)
        _, state = await pipeline.run(messages("我需要一点时间"), session_id="s", state=state, turn_id="wait")
        self.assertEqual(len(runner.calls_for("quizzer")), 1)
        self.assertEqual(state["quiz"]["asked"], 1)
        self.assertEqual(state["quiz"]["answered"], 0)
        self.assertEqual(state["quiz"]["awaiting"]["answer_index"], 0)
        for payload in runner.calls_for("coordinator", "respond"):
            rendered = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("answer_index", rendered)
            self.assertNotIn("ANSWER_KEY_PRIVATE", rendered)
        routing = json.dumps(runner.calls_for("coordinator", "decide")[1], ensure_ascii=False)
        self.assertNotIn("answer_index", routing)
        self.assertNotIn("ANSWER_KEY_PRIVATE", routing)

    async def test_wrong_answer_gets_feedback_without_same_turn_new_question(self):
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("C", quiz_action="continue", quiz_topic="分数"),
        ])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        output, state = await pipeline.run(messages("B"), session_id="s", state=state, turn_id="wrong")
        self.assert_protocol(output)
        self.assertEqual(len(runner.calls_for("quizzer")), 1)
        self.assertEqual(state["quiz"]["answered"], 1)
        self.assertEqual(state["quiz"]["correct"], 0)
        self.assertIsNone(state["quiz"]["awaiting"])
        feedback = runner.calls_for("coordinator", "respond")[-1]["quiz"]
        self.assertFalse(feedback["feedback"]["correct"])
        self.assertTrue(feedback["needs_review"])
        self.assertNotIn("new_question", feedback)

    async def test_three_question_quiz_generates_one_at_a_time_and_finishes(self):
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            *[plan("C", quiz_action="continue", quiz_topic="分数") for _ in range(3)],
        ])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        self.assertEqual(state["quiz"]["total"], 3)
        for number in range(1, 4):
            output, state = await pipeline.run(messages("A"), session_id="s", state=state, turn_id=f"answer-{number}")
            self.assertEqual(state["quiz"]["answered"], number)
            self.assertEqual(len(runner.calls_for("quizzer")), min(number + 1, 3))
        self.assertEqual([call["count"] for call in runner.calls_for("quizzer")], [1, 1, 1])
        self.assertEqual(state["quiz"]["correct"], 3)
        self.assertTrue(state["quiz"]["finished"])
        self.assertIsNone(output["next_step"])
        self.assertEqual(state["quiz"]["question_history"], list(QUIZ_VARIANTS))
        self.assertEqual([call["previous_questions"] for call in runner.calls_for("quizzer")], [[], list(QUIZ_VARIANTS[:1]), list(QUIZ_VARIANTS[:2])])

    async def test_repeated_question_retries_once_and_presents_only_the_new_question(self):
        original = {**QUIZ, "question": "What fraction is one of four equal parts?"}
        duplicate = {**QUIZ, "question": "  WHAT FRACTION IS ONE OF FOUR EQUAL PARTS?  "}
        replacement = {**QUIZ, "question": QUIZ_VARIANTS[1]}
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("C", quiz_action="continue", quiz_topic="分数"),
        ], quiz=[original, duplicate, replacement])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        output, state = await pipeline.run(messages("A"), session_id="s", state=state, turn_id="answer")
        self.assert_protocol(output)
        self.assertEqual(state["quiz"]["answered"], 1)
        self.assertEqual(state["quiz"]["asked"], 2)
        self.assertEqual(state["quiz"]["question_history"], [original["question"], replacement["question"]])
        self.assertIn(replacement["question"], output["next_step"]["text"])
        self.assertNotIn(duplicate["question"].strip(), json.dumps(output))
        generation = runner.calls_for("quizzer")
        self.assertEqual(len(generation), 3)
        self.assertTrue(all(call["count"] == 1 and call["require_new_question"] for call in generation))
        self.assertEqual(generation[1]["previous_questions"], [original["question"]])
        self.assertEqual(generation[2]["previous_questions"], [original["question"]])

    async def test_two_repeated_questions_fail_without_committing_answer_or_notes(self):
        self.store.set_enabled("s", True)
        duplicate = {**QUIZ, "question": f"  {QUIZ['question']}  "}
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数", observe_learning=True),
            plan("C", quiz_action="continue", quiz_topic="分数", observe_learning=True),
        ], quiz=[QUIZ, duplicate, QUIZ])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        before = deepcopy(state)
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            output, after = await pipeline.run(messages("A"), session_id="s", state=state, turn_id="answer")
        self.assert_protocol(output)
        self.assertEqual(output["intent"]["primary"], "G")
        self.assertEqual(after, before)
        self.assertEqual(after["quiz"]["answered"], 0)
        self.assertEqual(after["quiz"]["asked"], 1)
        self.assertEqual(after["quiz"]["awaiting"], QUIZ)
        self.assertEqual(len(runner.calls_for("quizzer")), 3)
        self.assertEqual(len(runner.calls_for("coordinator", "respond")), 1)
        # Notes are not compiled during a turn; the failed turn wrote nothing.
        self.assertEqual(runner.calls_for("notes"), [])
        self.assertEqual(self.store.snapshot("s")["concepts"], [])
        self.assertTrue(self.store.apply_notes("s", "answer", notes(), "A"))

    async def test_emotion_preserves_task_and_stop_clears_it(self):
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("E", pause_task=True, task_summary="准备考试"),
            plan("C", end_task=True, quiz_action="stop"),
        ])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        _, state = await pipeline.run(messages("好紧张"), session_id="s", state=state, turn_id="emotion")
        self.assertEqual(state["resume_stage"], "paused")
        self.assertIn("准备考试", state["pending_tasks"])
        self.assertEqual(state["quiz"]["asked"], 1)
        output, state = await pipeline.run(messages("停止测验"), session_id="s", state=state, turn_id="stop")
        self.assertIsNone(output["next_step"])
        self.assertNotIn("quiz", state)
        self.assertEqual(state["pending_tasks"], [])
        self.assertIsNone(state["active_task"])

    async def test_emotion_stop_action_clears_quiz_even_when_end_task_is_false(self):
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("E", quiz_action="stop", end_task=False, pause_task=True),
        ])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        output, state = await pipeline.run(messages("我累了，停止测验"), session_id="s", state=state, turn_id="stop")
        self.assert_protocol(output)
        self.assertEqual(output["intent"]["primary"], "E")
        self.assertIsNone(output["next_step"])
        self.assertNotIn("quiz", state)
        self.assertIsNone(state["active_task"])
        self.assertEqual(state["pending_tasks"], [])
        self.assertEqual(len(runner.calls_for("quizzer")), 1)

    async def test_failed_turn_preserves_newer_committed_progress_over_stale_caller_state(self):
        runner = FakeRunner([
            plan("C", quiz_action="start", quiz_topic="分数"),
            plan("C", quiz_action="continue", quiz_topic="分数"),
            plan("C", quiz_action="continue", quiz_topic="分数"),
        ])
        pipeline = self.pipeline(runner)
        _, old_state = await pipeline.run(messages("考考我"), session_id="s", turn_id="start")
        _, current_state = await pipeline.run(messages("A"), session_id="s", state=old_state, turn_id="first-answer")
        self.assertGreater(current_state["revision"], old_state["revision"])
        runner.replies.append(StageFailure("PRIVATE_FINAL_STAGE_ERROR"))
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            output, returned = await pipeline.run(messages("A"), session_id="s", state=old_state, turn_id="second-answer")
        self.assert_protocol(output)
        self.assertNotIn("PRIVATE_FINAL_STAGE_ERROR", output["message"])
        self.assertEqual(returned, current_state)
        self.assertEqual(returned["quiz"]["answered"], 1)
        self.assertEqual(returned["quiz"]["asked"], 2)
        self.assertEqual(old_state["quiz"]["answered"], 0)

    async def test_sqlite_read_failures_return_legal_fallback_without_advancing_state(self):
        self.store.set_enabled("s", True)
        for method in ("is_enabled", "snapshot"):
            with self.subTest(method=method):
                runner = FakeRunner()
                baseline = {"revision": 3, "active_task": "已有学习任务"}
                with patch.object(self.store, method, side_effect=StorageError("PRIVATE_DATABASE_DETAIL")), self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
                    output, state = await self.pipeline(runner).run(messages("继续"), session_id="s", state=baseline, turn_id=method)
                self.assert_protocol(output)
                self.assertEqual(output["intent"]["primary"], "G")
                self.assertNotIn("PRIVATE_DATABASE_DETAIL", output["message"])
                self.assertEqual(state, baseline)
                self.assertEqual(runner.calls, [])

    async def test_invalid_final_output_does_not_commit_artifact_notes_or_progress(self):
        self.store.set_enabled("s", True)
        invalid = {"secret": "PRIVATE_PROVIDER_TEXT"}
        runner = FakeRunner([plan(observe_learning=True, visual_request="展示分数", quiz_action="start", quiz_topic="分数")], replies=[invalid], note_results=[notes(True)])
        state_before = {"active_task": "之前的任务", "revision": 4}
        output, state = await self.pipeline(runner).run(messages("我在工作"), session_id="s", state=state_before, turn_id="bad")
        self.assert_protocol(output)
        self.assertEqual(output["intent"]["primary"], "G")
        self.assertEqual(state, state_before)
        self.assertNotIn("PRIVATE_PROVIDER_TEXT", json.dumps(output))
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        self.assertTrue(self.store.apply_notes("s", "bad", notes(), ""))

    async def test_unclear_intent_requires_question_instead_of_an_unrequested_task(self):
        wrong_step = {
            "schema_version": "1.0", "intent": {"primary": "G", "is_implicit": False},
            "message": "PRIVATE_WRONG_STEP", "next_step": {"kind": "task", "text": "先做二十道题"},
        }
        runner = FakeRunner([plan("G")], replies=[wrong_step])
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            output, state = await self.pipeline(runner).run(messages("这个"), session_id="s", turn_id="1")
        self.assert_protocol(output)
        self.assertEqual(output["next_step"]["kind"], "question")
        self.assertNotIn("PRIVATE_WRONG_STEP", output["message"])
        self.assertEqual(state, {})

    async def test_unretrieved_url_cannot_be_published_as_a_source(self):
        forged_source = {
            "schema_version": "1.0", "intent": {"primary": "A", "is_implicit": False},
            "message": "参考 [未经检索的页面](https://example.invalid/private-fabricated-source)",
            "next_step": {"kind": "question", "text": "理解了吗？"},
        }
        runner = FakeRunner([plan("A", visual_request="画图")], replies=[forged_source])
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            output, _ = await self.pipeline(runner).run(messages("解释一下"), session_id="s", turn_id="1")
        self.assert_protocol(output)
        self.assertNotIn("private-fabricated-source", json.dumps(output))
        self.assertEqual(self.artifacts.committed, [])

    async def test_invalid_or_unavailable_child_notes_do_not_leak_or_save(self):
        self.store.set_enabled("s", True)
        invalid = notes(True)
        invalid["profile"][0]["evidence_quote"] = "SECRET_FROM_UNRELATED_HISTORY"
        for number, child in enumerate((invalid, StageFailure("SECRET_PROVIDER_ERROR"))):
            with self.subTest(number=number):
                runner = FakeRunner(note_results=[child])
                output, state = await self.pipeline(runner).run(messages("整理笔记", *exchange("我在工作")),
                                                                session_id="s", turn_id=f"note-{number}")
                self.assert_protocol(output)
                self.assertIn("没有整理成功", output["message"])
                self.assertNotIn("notes_upto_turn", state.get("context", {}))
                self.assertNotIn("SECRET", json.dumps(output))
                self.assertEqual(self.store.snapshot("s")["profile"], {})

    async def test_unavailable_research_adds_uncertainty_and_prevents_visual(self):
        research = FakeResearch({"overall": "unavailable", "sources": [], "claims": []})
        runner = FakeRunner([plan(research_query="一个待核实知识点", visual_request="画出结论")])
        output, _ = await self.pipeline(runner, research).run(messages("解释一下"), session_id="s", turn_id="1")
        self.assert_protocol(output)
        self.assertIn("未完成检索验证", output["message"])
        self.assertIn("未生成可用的互动图", output["message"])
        self.assertEqual(runner.calls_for("visualizer"), [])
        self.assertEqual(self.artifacts.committed, [])

    async def test_invalid_visual_spec_is_not_published(self):
        runner = FakeRunner([plan(visual_request="展示关系")], visuals=[
            {"kind": "html", "content": "PRIVATE_MODEL_SCRIPT"},
            {"kind": "html", "content": "PRIVATE_MODEL_SCRIPT"}])
        output, _ = await self.pipeline(runner).run(messages("画图解释"), session_id="s", turn_id="1")
        self.assert_protocol(output)
        self.assertIn("未生成可用的互动图", output["message"])
        self.assertNotIn("PRIVATE_MODEL_SCRIPT", json.dumps(output))
        self.assertEqual(self.artifacts.committed, [])

    async def test_intent_cannot_change_after_routing_and_only_accepted_response_is_logged(self):
        rogue = {"schema_version": "1.0", "intent": {"primary": "F", "is_implicit": False}, "message": "PRIVATE_CHANGED_INTENT", "next_step": None}
        runner = FakeRunner([plan("A", research_query="分数定义")], replies=[rogue])
        with self.assertLogs("learning_coach.service.pipeline", level="INFO") as captured:
            output, state = await self.pipeline(runner).run(messages("请解释分数"), session_id="s", turn_id="bad")
        self.assertEqual(output["intent"]["primary"], "G")
        self.assertNotIn("PRIVATE_CHANGED_INTENT", json.dumps(output))
        self.assertFalse(any("intent_result" in record for record in captured.output))
        self.assertEqual(state, {})

    async def test_accepted_intent_log_is_single_public_source_and_replay_adds_none(self):
        expected = {"primary": "E", "is_implicit": True}
        runner = FakeRunner([plan("E", intent=expected)])
        pipeline = self.pipeline(runner)
        with self.assertLogs("learning_coach.service.pipeline", level="INFO") as captured:
            output, _ = await pipeline.run(messages("PRIVATE_LEARNER_EMOTION"), session_id="s", turn_id="raw-turn-id")
            await pipeline.run(messages("PRIVATE_LEARNER_EMOTION"), session_id="s", turn_id="raw-turn-id")
        events = [record.getMessage() for record in captured.records if record.getMessage().startswith("intent_result ")]
        self.assertEqual(len(events), 1)
        logged = json.loads(events[0].split(" ", 1)[1])
        self.assertEqual(logged["intent"], output["intent"])
        self.assertEqual(logged["intent"], expected)
        self.assertNotIn("PRIVATE_LEARNER_EMOTION", events[0])
        self.assertNotIn("raw-turn-id", events[0])


if __name__ == "__main__":
    unittest.main()
