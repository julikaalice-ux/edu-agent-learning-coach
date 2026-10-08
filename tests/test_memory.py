"""Offline tests for opt-in learning memory and evidence-based score updates."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src/agent-framework-agent-with-local-tools-responses/learning_coach/memory.py"
spec = importlib.util.spec_from_file_location("learning_memory_under_test", SOURCE)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


def notes():
    return {"schema_version": "1.0", "profile": [], "concepts": [], "error_records": [], "review_plan": []}


def profile_notes(origin="self_report"):
    result = notes()
    result["profile"] = [{"key": "learning_stage", "value": "职场", "origin": origin, "evidence_quote": "我在工作"}]
    return result


def evidence(correct=True, difficulty="简单", **kwargs):
    return {"concept_id": "fractions", "correct": correct, "difficulty": difficulty, "independent": True, **kwargs}


class LearningStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = memory.LearningStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enable(self, session="s"):
        self.store.set_enabled(session, True)

    def apply(self, turn, ev=None, content=None, learner_text="我在工作"):
        return self.store.apply_notes("s", turn, content or notes(), learner_text, ev)

    def mastery(self):
        return self.store.snapshot("s")["concepts"][0]["mastery_level"]

    def initialize(self):
        self.enable()
        self.apply("baseline", evidence(kind="restatement"))

    def test_disabled_by_default_does_not_record_content_or_turn_id(self):
        self.assertFalse(self.store.is_enabled("s"))
        self.assertFalse(self.apply("1", content=profile_notes()))
        with closing(sqlite3.connect(self.path)) as db, db:
            for table in ("profiles", "concepts", "applied_turns", "error_records", "review_items", "quiz_events"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.enable()
        self.assertTrue(self.apply("1", content=profile_notes()))

    def test_sessions_are_isolated_and_profile_provenance_preserved(self):
        self.enable()
        self.enable("other")
        self.apply("1", content=profile_notes("companion_report"))
        snapshot = self.store.snapshot("s")
        self.assertEqual(snapshot["profile"]["learning_stage"]["origin"], "companion_report")
        self.assertEqual(snapshot["profile"]["learning_stage"]["evidence_quote"], "我在工作")
        self.assertEqual(self.store.snapshot("other")["profile"], {})
        self.assertTrue(self.store.apply_notes("other", "1", notes(), ""))

    def test_disabling_hides_retained_data_and_stops_recording(self):
        self.enable()
        self.apply("1", content=profile_notes())
        self.store.set_enabled("s", False)
        self.assertFalse(self.apply("2", content=profile_notes()))
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        self.enable()
        self.assertIn("learning_stage", self.store.snapshot("s")["profile"])

    def test_clear_deletes_all_session_content_deduplication_and_consent(self):
        self.initialize()
        self.apply("1", content=profile_notes())
        self.enable("other")
        self.store.apply_notes("other", "1", profile_notes(), "我在工作")
        self.store.clear("s")
        self.assertFalse(self.store.is_enabled("s"))
        with closing(sqlite3.connect(self.path)) as db, db:
            for table in ("profiles", "concepts", "applied_turns", "error_records", "review_items", "quiz_events"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table} WHERE session_id = ?", ("s",)).fetchone()[0], 0)
        self.assertIn("learning_stage", self.store.snapshot("other")["profile"])
        self.enable()
        self.assertTrue(self.apply("1", content=profile_notes()))

    def test_same_turn_is_idempotent_even_across_store_instances(self):
        self.initialize()
        self.assertTrue(self.apply("1", evidence()))
        second = memory.LearningStore(self.path)
        try:
            self.assertFalse(second.apply_notes("s", "1", profile_notes(), "我在工作", evidence()))
        finally:
            second.close()
        self.assertEqual(self.mastery(), 0.4)
        self.assertEqual(self.store.snapshot("s")["profile"], {})

    def test_concurrent_duplicate_turn_only_scores_once(self):
        self.initialize()
        second = memory.LearningStore(self.path)
        barrier = Barrier(2)

        def submit(store):
            barrier.wait()
            return store.apply_notes("s", "concurrent", notes(), "我的答案是二分之一", evidence())

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(submit, (self.store, second)))
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(self.mastery(), 0.4)
        finally:
            second.close()

    def test_invalid_evidence_rolls_back_preceding_profile_write_and_turn(self):
        self.enable()
        with self.assertRaises(ValueError):
            self.apply("1", evidence(difficulty="bad"), profile_notes())
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        self.assertTrue(self.apply("1", content=profile_notes()))

    def test_quote_must_be_nonempty_and_in_current_learner_message(self):
        self.enable()
        for quote in ("", "过去消息", " "):
            invalid = profile_notes()
            invalid["profile"][0]["evidence_quote"] = quote
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.apply("1", content=invalid)
        self.assertTrue(self.apply("1", content=profile_notes()))

    def test_model_cannot_supply_mastery_path_or_extra_fields(self):
        self.enable()
        for key in ("mastery_level", "db_path", "session_id"):
            invalid = notes()
            invalid[key] = "untrusted"
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.apply("1", content=invalid)
        self.assertTrue(self.apply("1"))

    def test_unknown_graph_and_unconfirmed_error_are_preserved(self):
        self.enable()
        content = notes()
        content["concepts"] = [{"concept_id": "fractions", "label": "分数", "prerequisites": None, "related": None, "evidence_quote": "分数不会"}]
        content["error_records"] = [{"concept_id": "fractions", "category": "careless", "cause_confirmed": False, "evidence_quote": "算错了"}]
        content["review_plan"] = [{"topic": "分数", "deadline": None, "evidence_quote": "我要复习分数"}]
        self.apply("1", content=content, learner_text="分数不会，算错了，我要复习分数")
        snapshot = self.store.snapshot("s")
        self.assertIsNone(snapshot["concepts"][0]["prerequisites"])
        self.assertIsNone(snapshot["concepts"][0]["mastery_level"])
        self.assertEqual(snapshot["error_records"][0]["category"], "unclassified")
        self.assertIsNone(snapshot["review_plan"][0]["deadline"])

    def test_trusted_quiz_evidence_starts_mastery_from_neutral_floor(self):
        self.enable()
        self.apply("1", evidence())
        self.assertEqual(self.mastery(), 0.1)
        self.assertEqual(self.store.snapshot("s")["concepts"][0]["attempts"], 1)

    def test_first_quiz_failure_stays_at_floor(self):
        self.enable()
        self.apply("1", evidence(correct=False))
        self.assertEqual(self.mastery(), 0.0)

    def test_quiz_only_progression_without_restatement_baseline(self):
        self.enable()
        self.apply("1", evidence(difficulty="困难"))
        self.apply("2", evidence(difficulty="中等"))
        self.assertEqual(self.mastery(), 0.35)
        self.apply("3", evidence(correct=False))
        self.assertEqual(self.mastery(), 0.2)

    def test_restatement_after_quiz_floor_does_not_inflate(self):
        self.enable()
        self.apply("1", evidence())
        self.apply("2", evidence(kind="restatement"))
        self.assertEqual(self.mastery(), 0.1)

    def test_difficulty_updates_and_bounds(self):
        self.initialize()
        for turn, level, expected in (("1", "简单", 0.4), ("2", "中等", 0.55), ("3", "困难", 0.75), ("4", "困难", 0.95), ("5", "困难", 1.0)):
            self.apply(turn, evidence(difficulty=level))
            self.assertEqual(self.mastery(), expected)
        for index in range(10):
            self.apply(f"wrong-{index}", evidence(correct=False))
        self.assertEqual(self.mastery(), 0.0)

    def test_repeated_confirmed_same_error_gets_extra_penalty(self):
        self.initialize()
        self.apply("up", evidence(difficulty="困难"))
        self.apply("w1", evidence(correct=False, error_category="conceptual"))
        self.assertEqual(self.mastery(), 0.35)
        self.apply("w2", evidence(correct=False, error_category="conceptual"))
        self.assertEqual(self.mastery(), 0.1)

    def test_unclassified_or_different_errors_do_not_fake_repeated_cause(self):
        self.initialize()
        self.apply("up", evidence(difficulty="困难"))
        self.apply("w1", evidence(correct=False))
        self.apply("w2", evidence(correct=False))
        self.assertEqual(self.mastery(), 0.2)

    def test_companion_skip_silence_and_nonindependent_do_not_score(self):
        self.initialize()
        for index, ev in enumerate((None, evidence(actor="companion"), evidence(independent=False), evidence(skipped=True), evidence(independent="true"))):
            self.apply(str(index), ev)
        self.apply("silent", evidence(), learner_text=" \n")
        self.assertEqual(self.mastery(), 0.3)
        self.assertEqual(self.store.snapshot("s")["concepts"][0]["attempts"], 0)

    def test_repeated_restatement_cannot_inflate_mastery(self):
        self.initialize()
        self.apply("2", evidence(kind="restatement"))
        self.assertEqual(self.mastery(), 0.3)

    def test_database_version_is_checked(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE metadata SET value = '999' WHERE key = 'database_version'")
        with self.assertRaisesRegex(ValueError, "version"):
            memory.LearningStore(self.path)

    def test_schema_and_runtime_reject_wrong_version_and_bool_types(self):
        self.enable()
        invalid = notes()
        invalid["schema_version"] = "2.0"
        with self.assertRaises(ValueError):
            self.apply("1", content=invalid)
        with self.assertRaises(ValueError):
            self.store.set_enabled("s", "false")
        with self.assertRaises(ValueError):
            self.apply("1", evidence(correct="true"))
        self.assertTrue(self.apply("1"))


if __name__ == "__main__":
    unittest.main()
