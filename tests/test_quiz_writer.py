"""The quiz writer: diagnosed options, graded check questions, pictures, skipping."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.adapters.runner import StageFailure  # noqa: E402
from learning_coach.db.memory import LearningStore  # noqa: E402
from learning_coach.domain.contracts import QUIZ_SCHEMA, check_question, validate  # noqa: E402
from learning_coach.service.pipeline import NOTES_OFFER, LearningPipeline  # noqa: E402
from test_pipeline import (QUIZ, RESPONSE_SCHEMA, FakeArtifacts, FakeResearch, FakeRunner,  # noqa: E402
                           exchange, messages, plan)

THREE = {
    "concept_id": "fraction-size", "question": "哪个分数更大？", "choices": ["1/3", "1/2", "1/4"],
    "answer_index": 1, "explanation": "ANSWER_KEY_PRIVATE: 分母越小，每一份越大。",
    "diagnoses": [{"choice_index": 0, "error_type": "conceptual", "misconception": "PRIVATE_DIAGNOSIS: 以为分母越大分数越大"},
                  {"choice_index": 2, "error_type": "conceptual", "misconception": "PRIVATE_DIAGNOSIS: 以为分母越大分数越大"}],
    "image": None,
}


def direct(message="分数表示把整体平均分成几份，取其中几份。", check=None, primary="A"):
    return {"mode": "answer", "response": {
        "schema_version": "1.0", "intent": {"primary": primary, "is_implicit": False},
        "message": message, "next_step": {"kind": "question", "text": "PRIVATE_OWN_QUESTION 你听懂了吗？"}},
        "task": {"task_summary": "学习分数", "pause_task": False, "end_task": False},
        "check_question": check}


class Pictures:
    def __init__(self, fail=False):
        self.prompts, self.fail = [], fail

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_ERROR")
        return b"\x89PNG fake"


class QuizContractTests(unittest.TestCase):
    def test_every_wrong_choice_needs_exactly_one_diagnosis(self):
        self.assertIsNone(check_question(THREE))
        missing = deepcopy(THREE)
        missing["diagnoses"].pop()
        self.assertEqual(check_question(missing), "every_wrong_choice_needs_one_diagnosis")
        on_answer = deepcopy(THREE)
        on_answer["diagnoses"][0]["choice_index"] = 1
        self.assertEqual(check_question(on_answer), "every_wrong_choice_needs_one_diagnosis")
        out_of_range = {**THREE, "answer_index": 3}
        self.assertEqual(check_question(out_of_range), "answer_index_out_of_range")

    def test_only_the_four_error_types_are_accepted(self):
        validate(THREE, QUIZ_SCHEMA)
        careless = deepcopy(THREE)
        careless["diagnoses"][0]["error_type"] = "careless"
        with self.assertRaises(Exception):
            validate(careless, QUIZ_SCHEMA)


class QuizWriterPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "notes.sqlite3"
        self.store = LearningStore(self.path)
        self.artifacts = FakeArtifacts()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, pictures=None):
        return LearningPipeline(runner, FakeResearch(), self.store, self.artifacts, RESPONSE_SCHEMA,
                                image_generator=pictures)

    async def test_explanation_gets_a_graded_check_question_from_the_quiz_writer(self):
        self.store.set_enabled("s", True)
        runner = FakeRunner([direct(check={"topic": "分数的意义", "difficulty": "简单"})], quiz=[QUIZ])
        pipeline = self.pipeline(runner)
        output, state = await pipeline.run(messages("分数是什么"), session_id="s", turn_id="1")
        request = runner.calls_for("quizzer")[0]
        self.assertEqual((request["kind"], request["topic"]), ("check", "分数的意义"))
        self.assertEqual(request["explanation"], "分数表示把整体平均分成几份，取其中几份。")
        self.assertIn("A. 四分之一", output["next_step"]["text"])
        self.assertNotIn("PRIVATE", json.dumps(output, ensure_ascii=False))
        self.assertEqual((state["quiz"]["kind"], state["quiz"]["total"]), ("check", 1))
        self.assertEqual(len(runner.calls_for("coordinator")), 1)  # no extra coordinator call
        # Answering it is graded by the program and counts toward mastery.
        answered, after = await pipeline.run(messages("A", *exchange("分数是什么")), session_id="s",
                                             state=state, turn_id="2")
        concept = self.store.snapshot("s")["concepts"][0]
        self.assertEqual((concept["attempts"], concept["correct_count"]), (1, 1))
        self.assertTrue(after["quiz"]["finished"])
        self.assertNotEqual(answered["next_step"], {"kind": "question", "text": NOTES_OFFER})
        self.assertNotIn("notes_offer", after)
        self.assertEqual(after["context"]["quiz_results"][-1]["kind"], "check")

    async def test_wrong_answer_carries_the_suspected_error_and_repeats_are_flagged(self):
        self.store.set_enabled("s", True)
        again = direct(check={"topic": "分数大小", "difficulty": "简单"})
        # The answer turns also ask the coordinator first; the program then routes to grading.
        runner = FakeRunner([again, again, again], quiz=[THREE, {**THREE, "question": "哪个分数更大？（再看一次）"}])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("分数怎么比大小"), session_id="s", turn_id="1")
        self.assertNotIn("PRIVATE_DIAGNOSIS", json.dumps([c["payload"] for c in runner.calls if c["role"] == "coordinator"],
                                                         ensure_ascii=False))
        _, state = await pipeline.run(messages("A", *exchange("分数怎么比大小")), session_id="s", state=state, turn_id="2")
        feedback = runner.calls_for("coordinator", "respond")[-1]["quiz"]["feedback"]
        self.assertFalse(feedback["correct"])
        self.assertEqual((feedback["suspected_error"]["type"], feedback["suspected_error"]["code"]), ("概念性误解", "conceptual"))
        self.assertNotIn("repeated_error", feedback)
        _, state = await pipeline.run(messages("再讲讲", *exchange("分数怎么比大小", "A")), session_id="s",
                                      state=state, turn_id="3")
        _, state = await pipeline.run(messages("C", *exchange("分数怎么比大小", "A", "再讲讲")), session_id="s",
                                      state=state, turn_id="4")
        feedback = runner.calls_for("coordinator", "respond")[-1]["quiz"]["feedback"]
        self.assertTrue(feedback["repeated_error"])
        with sqlite3.connect(self.path) as db:
            categories = [row[0] for row in db.execute("SELECT error_category FROM quiz_events ORDER BY id")]
        self.assertEqual(categories, ["conceptual", "conceptual"])

    async def test_check_is_skipped_during_a_practice_set_and_falls_back_when_writing_fails(self):
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数"),
                             direct(check={"topic": "分数", "difficulty": "简单"})])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="p", turn_id="1")
        _, after = await pipeline.run(messages("顺便讲讲分母", *exchange("考考我")), session_id="p", state=state, turn_id="2")
        self.assertEqual(len(runner.calls_for("quizzer")), 1)
        self.assertEqual(after["quiz"]["kind"], "practice")
        failing = FakeRunner([direct(check={"topic": "分数", "difficulty": "简单"})], quiz=[StageFailure("PRIVATE")])
        output, state = await self.pipeline(failing).run(messages("分数是什么"), session_id="f", turn_id="1")
        self.assertIn("你听懂了吗", output["next_step"]["text"])
        self.assertNotIn("quiz", state)

    async def test_bad_diagnoses_are_corrected_once_then_rejected(self):
        bad = deepcopy(THREE)
        bad["diagnoses"].pop()
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数")], quiz=[bad, THREE])
        _, state = await self.pipeline(runner).run(messages("考考我"), session_id="d", turn_id="1")
        second = runner.calls_for("quizzer")[1]
        self.assertEqual(second["correction"], "every_wrong_choice_needs_one_diagnosis")
        self.assertEqual(state["quiz"]["awaiting"]["question"], THREE["question"])
        twice = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数")], quiz=[bad, bad])
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            output, state = await self.pipeline(twice).run(messages("考考我"), session_id="d2", turn_id="1")
        self.assertEqual(output["intent"]["primary"], "G")

    async def test_requested_picture_is_drawn_and_published_with_the_question(self):
        pictured = {**QUIZ, "question": "看图，图里是什么季节？", "choices": ["冬天", "夏天"],
                    "diagnoses": [{"choice_index": 1, "error_type": "conceptual", "misconception": "把雪景当成夏天"}],
                    "image": {"prompt": "a snowy village street, children building a snowman", "alt": "下雪的村庄"}}
        pictures = Pictures()
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="季节")], quiz=[pictured])
        output, state = await self.pipeline(runner, pictures).run(messages("考考我季节"), session_id="i", turn_id="1")
        self.assertEqual(pictures.prompts, ["a snowy village street, children building a snowman"])
        self.assertEqual(self.artifacts.prepared[0][1]["kind"], "illustration")
        self.assertEqual(len(self.artifacts.committed), 1)
        self.assertIn("[看题目配图](", output["message"])
        self.assertTrue(runner.calls_for("coordinator", "respond")[0]["quiz"]["new_question"]["has_picture"])

    async def test_picture_failure_asks_for_a_text_only_question(self):
        pictured = {**QUIZ, "image": {"prompt": "four equal slices of a round pie", "alt": "切成四块的饼"}}
        for pictures in (Pictures(fail=True), None):
            with self.subTest(generator=pictures):
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数")], quiz=[pictured, QUIZ])
                with self.assertLogs("learning_coach.service.pipeline", level="WARNING") if pictures else _quiet():
                    output, state = await self.pipeline(runner, pictures).run(messages("考考我"), session_id="t", turn_id="1")
                retry = runner.calls_for("quizzer")[1]
                self.assertTrue(retry["image_unavailable"])
                self.assertNotIn("看题目配图", output["message"])
                self.assertNotIn("PRIVATE_PROVIDER_ERROR", json.dumps(output, ensure_ascii=False))
                self.assertEqual(self.artifacts.committed, [])

    async def test_skip_replaces_the_question_without_grading(self):
        runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数")])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("考考我"), session_id="k", turn_id="1")
        first = state["quiz"]["awaiting"]["question"]
        output, after = await pipeline.run(messages("换一题", *exchange("考考我")), session_id="k", state=state, turn_id="2")
        self.assertIn("不算分", output["message"])
        self.assertNotEqual(after["quiz"]["awaiting"]["question"], first)
        self.assertEqual((after["quiz"]["answered"], after["quiz"]["skipped"]), (0, 1))
        self.assertEqual(len(runner.calls_for("coordinator", "decide")), 1)
        failing = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数")], quiz=[QUIZ, StageFailure("x")])
        pipeline = self.pipeline(failing)
        _, state = await pipeline.run(messages("考考我"), session_id="k2", turn_id="1")
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING"):
            output, after = await pipeline.run(messages("换一题", *exchange("考考我")), session_id="k2",
                                               state=state, turn_id="2")
        self.assertEqual(after["quiz"]["awaiting"], state["quiz"]["awaiting"])
        self.assertIn(QUIZ["question"], output["next_step"]["text"])

    async def test_practice_request_replaces_an_unanswered_check(self):
        runner = FakeRunner([direct(check={"topic": "分数", "difficulty": "简单"}),
                             plan("C", quiz_action="start", quiz_topic="分数比较", quiz_total=2)])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages("分数是什么"), session_id="r", turn_id="1")
        _, after = await pipeline.run(messages("出两道题练练", *exchange("分数是什么")), session_id="r",
                                      state=state, turn_id="2")
        self.assertEqual((after["quiz"]["kind"], after["quiz"]["topic"], after["quiz"]["total"]), ("practice", "分数比较", 2))


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class LegacyErrorTypeTests(unittest.TestCase):
    def test_old_error_names_are_converted_when_the_store_opens(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "m.sqlite3"
            LearningStore(path).close()
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO error_records (session_id, turn_id, concept_id, category, evidence_quote) "
                           "VALUES ('s', 't', 'c', 'careless', 'q')")
                db.execute("INSERT INTO quiz_events (session_id, turn_id, concept_id, kind, correct, difficulty, error_category) "
                           "VALUES ('s', 't', 'c', 'quiz', 0, '简单', 'procedural')")
            LearningStore(path).close()
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT category FROM error_records").fetchone()[0], "misreading")
                self.assertEqual(db.execute("SELECT error_category FROM quiz_events").fetchone()[0], "step_skipping")


if __name__ == "__main__":
    unittest.main()
