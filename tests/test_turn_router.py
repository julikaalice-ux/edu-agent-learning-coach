"""The program's judgement before any model call: record commands, then the phase table."""

from pathlib import Path
import tempfile
import unittest

from test_pipeline import FakeArtifacts, FakeResearch, FakeRunner, LearningPipeline, LearningStore, RESPONSE_SCHEMA, \
    messages, plan
from learning_coach.service.pipeline import router
from learning_coach.service.pipeline.router import Action, Command, Phase
from learning_coach.service.pipeline.texts import CHECK_OFFER

WAITING = {"quiz": {"topic": "分数", "total": 3, "answered": 0, "finished": False,
                    "awaiting": {"question": "?", "choices": ["a", "b"], "answer_index": 0}}}


class RouterTests(unittest.TestCase):
    def test_record_commands_apply_in_any_phase(self):
        self.assertIs(router.record_command("关闭记录", False), Command.RECORDS_OFF)
        self.assertIs(router.record_command("/memory on", False), Command.RECORDS_ON)
        self.assertIs(router.record_command("删除记录", False), Command.ASK_DELETE)
        # "确定删除" deletes only right after the program asked.
        self.assertIsNone(router.record_command("确定删除", False))
        self.assertIs(router.record_command("确定删除", True), Command.FORGET)
        self.assertIsNone(router.record_command("什么是分数", False))

    def test_phases_stack_a_one_turn_offer_on_where_the_lesson_is(self):
        self.assertEqual(router.phases({}, {}), {Phase.ANY, Phase.EXPLAIN})
        current = router.phases(WAITING, {"check_offer": {"topic": "分数"}})
        self.assertEqual(current, {Phase.ANY, Phase.OFFER_CHECK, Phase.QUIZ})

    def test_phase_table(self):
        explain = router.phases({}, {})
        self.assertIs(router.next_action("整理笔记", explain), Action.COMPILE_NOTES)
        self.assertIs(router.next_action("1", explain), Action.TEACH)
        self.assertIs(router.next_action("1", router.phases({}, {"notes_offer": True})), Action.COMPILE_NOTES)
        self.assertIs(router.next_action("不用", router.phases({}, {"notes_offer": True})), Action.DECLINE_NOTES)
        offer = router.phases({}, {"check_offer": {"topic": "分数"}})
        self.assertIs(router.next_action("1", offer), Action.ASK_CHECK)
        self.assertIs(router.next_action("先不用", offer), Action.DECLINE_CHECK)
        self.assertIs(router.next_action("分数怎么比大小", offer), Action.TEACH)
        # An unanswered offer does not hide the question still waiting underneath.
        both = router.phases(WAITING, {"check_offer": {"topic": "分数"}})
        self.assertIs(router.next_action("换一题", both), Action.REPLACE_QUESTION)
        self.assertIs(router.next_action("换一题", explain), Action.TEACH)


class OneTurnOfferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    async def test_an_offer_expires_even_when_the_next_turn_is_a_record_command(self):
        runner = FakeRunner([plan("A", check_question={"topic": "分数", "difficulty": "简单"}), plan("E")])
        pipeline = LearningPipeline(runner, FakeResearch(), self.store, FakeArtifacts(), RESPONSE_SCHEMA)
        output, state = await pipeline.run(messages("什么是分数"), session_id="s", turn_id="1")
        self.assertEqual(output["next_step"]["text"], CHECK_OFFER)
        _, state = await pipeline.run(messages("关闭记录"), session_id="s", state=state, turn_id="2")
        self.assertNotIn("check_offer", state)
        _, state = await pipeline.run(messages("1"), session_id="s", state=state, turn_id="3")
        # "1" is taught, not taken as yes to the earlier offer: no question was written.
        self.assertEqual(runner.calls_for("quizzer"), [])
        self.assertEqual(len(runner.calls_for("coordinator", "decide")), 2)


if __name__ == "__main__":
    unittest.main()
