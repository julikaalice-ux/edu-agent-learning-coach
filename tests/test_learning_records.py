"""Mistakes and study notes: one card per concept, nothing skipped, a bad entry dropped alone."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.db.memory import LearningStore
from learning_coach.domain.context import Budgets
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.pipeline.router import Action, next_action, phases
from test_pipeline import FakeArtifacts, FakeResearch, FakeRunner, RESPONSE_SCHEMA, notes

QUESTION = {"question": "2/7 + 3/7 = ?", "choices": ["5/7", "5/14", "6/7"], "answer_index": 0,
            "explanation": "分母不变，分子相加。", "concept_id": "fraction-addition"}
STUDY = {"concept_id": "fraction-addition", "title": "同分母分数加法", "summary": "分母不变，分子相加。",
         "explanation": "每份大小相同，只是份数合起来。", "example": "2/7+3/7=5/7", "pitfall": "不能把分母也相加",
         "conditions": "分母相同", "source_turns": [1]}
EDIT_FIELDS = ("title", "summary", "explanation", "example", "pitfall", "conditions")


def history(turns):
    items = []
    for number in range(1, turns + 1):
        items += [{"role": "user", "text": f"第{number}个问题"}, {"role": "assistant", "text": f"第{number}段讲解。" * 40}]
    return items


class RecordsTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")
        self.store.set_enabled("s", True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, budgets=None):
        return LearningPipeline(runner, FakeResearch(), self.store, FakeArtifacts(), RESPONSE_SCHEMA, budgets=budgets)

    def seed_mistake(self):
        evidence = {"kind": "quiz", "correct": False, "independent": True, "actor": "learner", "difficulty": "简单",
                    "concept_id": QUESTION["concept_id"], "question_snapshot": QUESTION, "selected": 1,
                    "topic": "分数加法"}
        self.store.apply_notes("s", "wrong-1", notes(), "B", evidence)

    async def compile(self, runner, messages, **kwargs):
        return await self.pipeline(runner, kwargs.pop("budgets", None)).run(
            messages + [{"role": "user", "text": "整理笔记"}], session_id="s", returning_visitor=True, **kwargs)


class OneCardPerConceptTests(RecordsTestBase):
    async def test_compiling_a_concept_again_updates_its_card(self):
        self.store.apply_notes("s", "notes:1", {**notes(), "study_notes": [STUDY]}, "")
        first = self.store.learning_view("s")["study_notes"][0]
        self.store.apply_notes("s", "notes:2", {**notes(), "study_notes": [{**STUDY, "summary": "新的结论"}]}, "")
        cards = self.store.learning_view("s")["study_notes"]
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["id"], cards[0]["created_at"]), (first["id"], first["created_at"]))
        self.assertEqual(cards[0]["summary"], "新的结论")

    async def test_a_card_the_learner_edited_is_kept_and_the_reply_says_so(self):
        self.store.apply_notes("s", "notes:1", {**notes(), "study_notes": [STUDY]}, "")
        card = self.store.learning_view("s")["study_notes"][0]
        self.store.edit_study_note("s", card["id"], {**{key: card[key] for key in EDIT_FIELDS}, "summary": "我自己的话"})
        runner = FakeRunner(note_results=[{**notes(), "study_notes": [{**STUDY, "summary": "模型的新版本"}]}])
        output, _ = await self.compile(runner, history(1))
        cards = self.store.learning_view("s")["study_notes"]
        self.assertEqual([item["summary"] for item in cards], ["我自己的话"])
        self.assertTrue(cards[0]["edited"])
        self.assertIn("保留了你的版本", output["message"])


class NothingSkippedTests(RecordsTestBase):
    async def test_a_long_conversation_is_compiled_in_parts_from_the_oldest_turn(self):
        budgets = Budgets(notes_messages=60, notes_teaching=600)   # two explanations per compile
        runner = FakeRunner(note_results=[notes() for _ in range(12)])
        messages, state, seen = history(9), {}, []
        for _ in range(12):
            output, state = await self.compile(runner, messages, state=state, budgets=budgets)
            request = runner.calls_for("notes")[-1]
            seen += [item["turn"] for item in request["teaching_messages"]]
            if "接着整理" not in output["message"]:
                break
            messages = messages + [{"role": "user", "text": "整理笔记"}, {"role": "assistant", "text": output["message"]}]
        first = runner.calls_for("notes")[0]
        self.assertEqual(first["teaching_messages"][0]["turn"], 1)
        self.assertEqual(first["learner_messages"][0]["turn"], 1)
        # Every explanation was given exactly once, in order, across the compiles.
        self.assertEqual([turn for turn in seen if turn <= 9], list(range(1, 10)))
        self.assertGreater(len(runner.calls_for("notes")), 1)
        self.assertNotIn("接着整理", output["message"])


class DropOnlyTheBadEntryTests(RecordsTestBase):
    async def test_one_unquoted_entry_does_not_cost_the_rest(self):
        draft = {**notes(), "study_notes": [STUDY],
                 "profile": [{"key": "learning_stage", "value": "初中", "origin": "self_report",
                              "evidence_quote": "学习者没有说过的话"}],
                 "concepts": [{"concept_id": "fraction-addition", "label": "分数加法", "prerequisites": None,
                               "related": None, "evidence_quote": "第1个问题"}]}
        runner = FakeRunner(note_results=[draft])
        output, state = await self.compile(runner, history(1))
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        self.assertEqual([item["concept_id"] for item in self.store.snapshot("s")["concepts"]], ["fraction-addition"])
        self.assertEqual(len(self.store.learning_view("s")["study_notes"]), 1)
        self.assertIn("有 1 条对不上", output["message"])
        self.assertEqual(state["context"]["notes_upto_turn"], 2)


class MistakesTests(RecordsTestBase):
    async def test_asking_for_mistakes_reports_the_saved_ones_without_compiling(self):
        self.seed_mistake()
        runner = FakeRunner()
        output, _ = await self.pipeline(runner).run([{"role": "user", "text": "整理错题"}], session_id="s",
                                                    returning_visitor=True)
        self.assertIn("一共 1 道", output["message"])
        self.assertIn(QUESTION["question"], output["message"])
        self.assertIn("错题集合", output["message"])
        self.assertNotIn(str(QUESTION["answer_index"]) + ".", output["message"].split("\n")[0])
        self.assertEqual(runner.calls, [])

    async def test_with_records_off_the_reply_says_nothing_is_saved(self):
        self.store.set_enabled("s", False)
        output, _ = await self.pipeline(FakeRunner()).run([{"role": "user", "text": "帮我整理错题"}], session_id="s",
                                                          returning_visitor=True)
        self.assertIn("没有开启学习记录", output["message"])

    def test_the_words_route_to_the_mistakes_not_to_notes(self):
        self.assertIs(next_action("整理错题", phases({}, {})), Action.SHOW_MISTAKES)
        self.assertIs(next_action("整理笔记", phases({}, {})), Action.COMPILE_NOTES)

    async def test_notes_reuse_the_concept_ids_of_saved_mistakes(self):
        self.seed_mistake()
        runner = FakeRunner(note_results=[notes()])
        await self.compile(runner, history(1))
        known = runner.calls_for("notes")[0]["known_concepts"]
        self.assertEqual(known, [{"concept_id": "fraction-addition", "title": "分数加法"}])


class StrictSchemaTests(unittest.TestCase):
    def test_native_requests_list_every_field_while_replies_keep_their_optional_fields(self):
        from learning_coach.adapters.runner import native_schema
        from learning_coach.domain.note_contracts import NOTES_SCHEMA
        from learning_coach.domain.visual_contracts import VISUAL_SCHEMA

        def objects(node):
            if isinstance(node, dict):
                if isinstance(node.get("properties"), dict):
                    yield node
                for value in node.values():
                    yield from objects(value)
            elif isinstance(node, list):
                for value in node:
                    yield from objects(value)

        for schema in (NOTES_SCHEMA, VISUAL_SCHEMA):
            for node in objects(native_schema(schema)):
                self.assertEqual(set(node["required"]), set(node["properties"]))
        self.assertNotIn("study_notes", NOTES_SCHEMA["required"])


if __name__ == "__main__":
    unittest.main()
