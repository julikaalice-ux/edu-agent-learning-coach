"""What each agent sees: token budgets, look-back, conversation notes, notes after a quiz."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.db.memory import LearningStore  # noqa: E402
from learning_coach.domain import context as ctx  # noqa: E402
from learning_coach.domain.contracts import decision_schema, validate  # noqa: E402
from learning_coach.service.pipeline import NOTES_OFFER, LearningPipeline  # noqa: E402
from test_pipeline import (RESPONSE_SCHEMA, FakeArtifacts, FakeResearch, FakeRunner,  # noqa: E402
                           exchange, messages, notes, plan)

PIZZA = "我们用披萨来想：一张披萨平均切成四块，拿走一块，就是四分之一。"


def conversation(turns, *, long_reply="这一步先看整体有几份，再看取了几份。" * 20):
    """Turn 3 uses the pizza example; every other reply is long filler."""
    history = []
    for number in range(1, turns + 1):
        history.append({"role": "user", "text": f"第{number}个问题：分数怎么理解"})
        history.append({"role": "assistant", "text": PIZZA if number == 3 else long_reply})
    return history


class TokenAndWindowTests(unittest.TestCase):
    def test_estimate_counts_chinese_by_character_and_other_text_by_four(self):
        self.assertEqual(ctx.estimate_tokens("分数"), 2)
        self.assertEqual(ctx.estimate_tokens("abcdefgh"), 2)

    def test_long_message_keeps_head_and_tail(self):
        text = "开头" + "中" * 5000 + "结尾"
        trimmed = ctx.trim_text(text, 300)
        self.assertTrue(trimmed.startswith("开头") and trimmed.endswith("结尾"))
        self.assertIn("中间省略约", trimmed)
        self.assertLessEqual(ctx.count_tokens(trimmed), 330)
        self.assertEqual(ctx.trim_text("短句", 300), "短句")

    def test_window_keeps_newest_turns_within_budget_and_never_fewer_than_minimum(self):
        numbered = ctx.number_turns(messages("现在的问题", *conversation(20)))
        window = ctx.recent_window(numbered, 1000, min_turns=3, message_max=1500)
        turns = sorted({item["turn"] for item in window})
        self.assertEqual(turns[-1], 21)
        self.assertGreaterEqual(len(turns), 3)
        self.assertEqual(turns, list(range(turns[0], 22)))
        tiny = ctx.recent_window(numbered, 1, min_turns=3, message_max=1500)
        self.assertEqual(sorted({item["turn"] for item in tiny}), [19, 20, 21])


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.numbered = ctx.number_turns(messages("我还是不懂", *exchange("分母是什么", "我觉得分母越大越大")))

    def test_notes_are_checked_capped_and_turns_corrected(self):
        proposed = {
            "covered": [{"point": f"讲过 {i}", "turn": 1} for i in range(9)],
            "stuck": [{"quote": "分母越大越大", "issue": "把分母大小当成分数大小", "turn": 1},
                      {"quote": "学习者从没说过的话", "issue": "编造", "turn": 2}],
            "requests": [{"text": "讲慢一点", "turn": 99}],
            "open_questions": [],
        }
        accepted, problems = ctx.accept_notes(proposed, None, self.numbered, 1000)
        self.assertEqual(len(accepted["covered"]), ctx.NOTE_LIMITS["covered"])
        self.assertEqual(accepted["covered"][-1]["point"], "讲过 8")
        self.assertEqual(accepted["stuck"], [{"quote": "分母越大越大", "issue": "把分母大小当成分数大小", "turn": 2}])
        self.assertEqual(accepted["requests"], [])
        self.assertIn("stuck_quote_not_said", problems)
        self.assertIn("requests_turn_unknown", problems)

    def test_missing_update_keeps_previous_notes(self):
        previous = {**ctx.empty_notes(), "requests": [{"text": "讲慢一点", "turn": 1}]}
        self.assertEqual(ctx.accept_notes(None, previous, self.numbered, 1000), (previous, []))

    def test_notes_are_cut_to_the_token_budget(self):
        proposed = {**ctx.empty_notes(), "covered": [{"point": "很长的要点" * 10, "turn": 1}] * 6,
                    "stuck": [{"quote": "分母越大越大", "issue": "错因", "turn": 2}]}
        accepted, _ = ctx.accept_notes(proposed, None, self.numbered, 120)
        self.assertLessEqual(ctx.count_tokens(accepted), 120)
        self.assertEqual(len(accepted["stuck"]), 1)  # learner difficulties go last

    def test_decision_schema_accepts_notes_or_no_change(self):
        schema = decision_schema(RESPONSE_SCHEMA)
        decision = {"mode": "answer", "response": {"schema_version": "1.0",
                    "intent": {"primary": "A", "is_implicit": False}, "message": "好", "next_step": None},
                    "task": {"task_summary": None, "pause_task": False, "end_task": False},
                    "context_notes": {**ctx.empty_notes(), "covered": [{"point": "披萨例子", "turn": 3}]},
                    "check_question": None}
        validate(decision, schema)
        validate({**decision, "context_notes": None}, schema)


class LearnerCardAndRecordsTests(unittest.TestCase):
    def test_stage_comes_from_onboarding_even_with_records_off(self):
        state = {"onboarding": {"stage": "初中", "who": "self"}, "context": {"goal": {"turn": 2, "text": "想学分数"}}}
        self.assertEqual(ctx.brief_card(state, {}, "learner"), {"stage": "初中", "goal": "想学分数", "participant": "learner"})
        card = ctx.learner_card(state, {}, memory_enabled=False)
        self.assertEqual((card["stage"], card["who"], card["records"]), ("初中", "learner", "off"))

    def test_memory_slice_prefers_related_then_weak_concepts_and_reports_what_is_hidden(self):
        concepts = [{"concept_id": f"c{i}", "label": f"其他知识{i}", "mastery_level": 0.9, "evidence_quote": "x" * 500}
                    for i in range(12)]
        concepts.append({"concept_id": "fraction", "label": "分数的意义", "mastery_level": 0.8, "evidence_quote": "y"})
        concepts.append({"concept_id": "weak", "label": "小数", "mastery_level": 0.1, "evidence_quote": "z"})
        memory = {"profile": {"learning_stage": {"value": "初中", "origin": "self_report", "evidence_quote": "q", "turn_id": "t"}},
                  "concepts": concepts, "error_records": [{"concept_id": "fraction", "category": "conceptual",
                                                           "evidence_quote": "e", "turn_id": "t"}] * 9,
                  "review_plan": []}
        view = ctx.memory_slice(memory, "讲讲分数的意义", 1500)
        self.assertEqual([c["concept_id"] for c in view["concepts"][:2]], ["fraction", "weak"])
        self.assertEqual(len(view["concepts"]), 8)
        self.assertNotIn("evidence_quote", view["concepts"][0])
        self.assertEqual(view["profile"]["learning_stage"], {"value": "初中", "origin": "self_report"})
        self.assertEqual(view["semantic"], [])
        self.assertEqual(view["not_shown"],
                         {"semantic": 0, "concepts": 6, "error_records": 4, "review_plan": 0})
        self.assertLessEqual(ctx.count_tokens(view), 1500)

    def test_memory_slice_folds_semantic_items_and_drops_them_first_on_budget(self):
        semantic = [{"id": f"s{i}", "content": f"语义记忆{i}"} for i in range(8)]
        memory = {"profile": {}, "semantic": semantic, "concepts": [], "error_records": [], "review_plan": []}
        view = ctx.memory_slice(memory, "分数", 1500)
        self.assertEqual([item["id"] for item in view["semantic"]], [f"s{i}" for i in range(5)])
        self.assertEqual(view["not_shown"]["semantic"], 3)
        # Content is capped defensively.
        long_memory = {"profile": {}, "semantic": [{"id": "long", "content": "长" * 500}],
                       "concepts": [], "error_records": [], "review_plan": []}
        self.assertEqual(ctx.memory_slice(long_memory, "分数", 1500)["semantic"][0]["content"], "长" * 300)
        # Semantic items are the first thing trimmed when over budget.
        tight = ctx.memory_slice(memory, "分数", 1)
        self.assertEqual(tight["semantic"], [])
        self.assertEqual(tight["not_shown"]["semantic"], 8)

    def test_research_digest_drops_page_excerpts(self):
        research = {"overall": "verified", "warnings": [],
                    "sources": [{"id": "s1", "title": "T", "url": "https://a.test", "publisher": "a.test",
                                 "excerpt": "原文" * 1500, "excerpt_kind": "page_extract"}],
                    "claims": [{"text": "结论", "status": "verified",
                                "evidence": [{"source_id": "s1", "quote": "引" * 800, "relation": "supports"}] * 4}]}
        digest = ctx.research_digest(research)
        self.assertNotIn("excerpt", digest["sources"][0])
        self.assertEqual(len(digest["claims"][0]["evidence"]), 2)
        self.assertEqual(len(digest["claims"][0]["evidence"][0]["quote"]), 300)
        self.assertIsNone(ctx.research_digest(None))

    def test_budgets_can_be_changed_by_environment(self):
        budgets = ctx.Budgets.from_env({"LEARNING_COACH_CTX_DECIDE_WINDOW": "6000",
                                        "LEARNING_COACH_CTX_MEMORY": "oops"})
        self.assertEqual((budgets.decide_window, budgets.memory), (6000, ctx.Budgets().memory))


class PipelineContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner):
        return LearningPipeline(runner, FakeResearch(), self.store, FakeArtifacts(), RESPONSE_SCHEMA)

    async def test_example_from_turn_three_reaches_turn_twenty_one_through_the_notes(self):
        """Turn 3 leaves the window, but the note written then still reaches the coordinator."""
        pizza = {**ctx.empty_notes(), "covered": [{"point": "分数的意义，用了披萨切四块的例子", "turn": 3}]}
        state = {"context": {"notes": pizza}}
        runner = FakeRunner([plan()])
        await self.pipeline(runner).run(messages("用刚才那个披萨的例子再讲一遍", *conversation(20)),
                                        session_id="long", state=state)
        decide = runner.calls_for("coordinator", "decide")[0]
        shown = {item["turn"] for item in decide["messages"]}
        self.assertNotIn(3, shown)
        self.assertIn(21, shown)
        self.assertEqual(decide["context_notes"]["covered"], pizza["covered"])
        self.assertLessEqual(ctx.count_tokens(decide["messages"]), ctx.Budgets().decide_window)
        self.assertNotIn("recalled", decide)
        self.assertNotIn("earlier", decide)
        self.assertEqual(len(runner.calls_for("coordinator", "decide")), 1)

    async def test_notes_from_the_coordinator_survive_into_later_turns(self):
        noting = {"covered": [{"point": "分数的意义，用了披萨例子", "turn": 1}], "stuck": [],
                  "requests": [{"text": "讲慢一点", "turn": 1}], "open_questions": []}
        runner = FakeRunner([{**self.direct(), "context_notes": noting}, self.direct()])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("讲慢一点，分数是什么"), session_id="n", turn_id="1")
        await pipeline.run(messages("继续", *exchange("讲慢一点，分数是什么")), session_id="n", state=state, turn_id="2")
        later = runner.calls_for("coordinator", "decide")[1]
        self.assertEqual(later["context_notes"]["requests"], [{"text": "讲慢一点", "turn": 1}])

    @staticmethod
    def direct():
        return {"mode": "answer", "response": {
            "schema_version": "1.0", "intent": {"primary": "A", "is_implicit": False},
            "message": "好的。", "next_step": {"kind": "question", "text": "继续吗？"}},
            "task": {"task_summary": "学习分数", "pause_task": False, "end_task": False}}

    async def finish_one_question_quiz(self, session, *, records):
        self.store.set_enabled(session, records)
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数", quiz_total=1, observe_learning=True),
                             plan("C", quiz_action="continue", quiz_topic="分数", quiz_total=1)],
                            note_results=[notes()])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("我在工作，考考我分数"), session_id=session, turn_id="1")
        history = exchange("我在工作，考考我分数")
        output, state = await pipeline.run(messages("A", *history), session_id=session, state=state, turn_id="2")
        return runner, pipeline, output, state, history + exchange("A")

    async def test_finished_quiz_offers_notes_and_yes_compiles_them(self):
        runner, pipeline, output, state, history = await self.finish_one_question_quiz("yes", records=True)
        self.assertEqual(output["next_step"]["text"], NOTES_OFFER)
        results = [{"topic": "分数", "kind": "practice", "answered": 1, "correct": 1, "suspected_errors": []}]
        self.assertEqual(state["context"]["quiz_results"], results)
        compiled, state = await pipeline.run(messages("整理", *history), session_id="yes", state=state, turn_id="3")
        request = runner.calls_for("notes")[0]
        self.assertEqual([item["turn"] for item in request["learner_messages"]], [1, 2])
        self.assertEqual(request["evidence_turns"], [1])
        self.assertEqual(request["quiz_results"], results)
        self.assertNotIn("notes_offer", state)
        self.assertEqual(state["context"]["notes_upto_turn"], 3)  # including the request itself
        # A second request has nothing new to compile.
        again, _ = await pipeline.run(messages("整理笔记", *history, *exchange("整理")), session_id="yes",
                                      state=state, turn_id="4")
        self.assertIn("还没有新的内容", again["message"])
        self.assertEqual(len(runner.calls_for("notes")), 1)

    async def test_declined_or_ignored_offer_compiles_nothing(self):
        runner, pipeline, _, state, history = await self.finish_one_question_quiz("no", records=True)
        declined, after = await pipeline.run(messages("不用", *history), session_id="no", state=state, turn_id="3")
        self.assertIn("先不整理", declined["message"])
        self.assertNotIn("notes_offer", after)
        _, _, _, state, history = await self.finish_one_question_quiz("moved-on", records=True)
        runner_two = FakeRunner([plan()])
        moved = self.pipeline(runner_two)
        await moved.run(messages("讲讲小数", *history), session_id="moved-on", state=state, turn_id="3")
        self.assertEqual(len(runner_two.calls_for("coordinator", "decide")), 1)
        self.assertEqual(runner.calls_for("notes"), [])

    async def test_records_off_never_offers_and_explains_on_request(self):
        runner, pipeline, output, state, history = await self.finish_one_question_quiz("off", records=False)
        self.assertIsNone(output["next_step"])
        reply, _ = await pipeline.run(messages("整理笔记", *history), session_id="off", state=state, turn_id="3")
        self.assertIn("没有开启学习记录", reply["message"])
        self.assertEqual(runner.calls_for("notes"), [])

    async def test_every_sub_agent_gets_the_learner_card(self):
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数", research_query="分数定义",
                                  visual_request="分数对照", quiz_uses_research=False, visual_uses_research=False)])
        research = FakeResearch()
        pipeline = LearningPipeline(runner, research, self.store, FakeArtifacts(), RESPONSE_SCHEMA)
        await pipeline.run(messages("我上初中，考考我分数"), session_id="card",
                           state={"onboarding": {"step": "done", "stage": "初中", "who": "self", "records": "off"}})
        self.assertEqual(research.queries[0], ["分数定义"])  # written by the coordinator
        quiz = runner.calls_for("quizzer")[0]
        self.assertEqual(quiz["learner"]["stage"], "初中")
        self.assertEqual(set(quiz["context_notes"]), {"covered", "stuck"})
        self.assertEqual(quiz["recent_messages"][-1]["text"], "我上初中，考考我分数")
        self.assertEqual(runner.calls_for("visualizer")[0]["learner"]["stage"], "初中")
        respond = runner.calls_for("coordinator", "respond")[0]
        self.assertEqual(respond["learner"]["stage"], "初中")
        self.assertEqual(respond["research"]["sources"], [])


if __name__ == "__main__":
    unittest.main()
