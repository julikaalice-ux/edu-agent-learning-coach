"""First-meeting questions, demo mode and plain-language record controls."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/agent-framework-agent-with-local-tools-responses"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.db.memory import LearningStore  # noqa: E402
from learning_coach.domain import onboarding  # noqa: E402
from learning_coach.service.pipeline import LearningPipeline  # noqa: E402
from test_pipeline import RESPONSE_SCHEMA, FakeArtifacts, FakeResearch, FakeRunner  # noqa: E402


class OnboardingRulesTests(unittest.TestCase):
    def test_greeting_is_not_kept_as_a_question_but_a_real_question_is(self):
        self.assertIsNone(onboarding.start("你好！")["pending_question"])
        self.assertEqual(onboarding.start("讲讲光合作用")["pending_question"], "讲讲光合作用")

    def test_numbers_and_option_text_are_both_accepted(self):
        record = onboarding.advance({"step": "who"}, "2")
        self.assertEqual((record["who"], record["step"]), ("companion", "stage_child"))
        record = onboarding.advance({"step": "stage_self"}, "大学")
        self.assertEqual((record["stage"], record["step"]), ("大学", "consent_self"))

    def test_possibly_under_14_needs_a_guardian(self):
        for stage in ("学龄前", "小学", "初中", "不想说"):
            with self.subTest(stage=stage):
                self.assertEqual(onboarding.advance({"step": "stage_self"}, stage)["step"], "guardian")
        absent = onboarding.advance({"step": "guardian"}, "2")
        self.assertEqual((absent["step"], absent["records"]), ("done", "off"))

    def test_unclear_answers_repeat_and_skip_turns_records_off(self):
        self.assertTrue(onboarding.advance({"step": "consent_self"}, "随便")["retry"])
        self.assertEqual(onboarding.advance({"step": "consent_self"}, "保存")["records"], "on")
        self.assertEqual(onboarding.advance({"step": "consent_self"}, "先不保存")["records"], "off")
        skipped = onboarding.advance({"step": "who"}, "跳过")
        self.assertEqual((skipped["step"], skipped["records"]), ("done", "off"))


class OnboardingPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, **options):
        return LearningPipeline(runner, FakeResearch(), self.store, FakeArtifacts(), RESPONSE_SCHEMA, **options)

    async def converse(self, pipeline, texts, session="s", **run_options):
        history, state, outputs = [], None, []
        for number, text in enumerate(texts):
            history.append({"role": "user", "text": text})
            output, state = await pipeline.run(list(history), session_id=session, state=state,
                                               turn_id=str(number), **run_options)
            outputs.append(output)
            shown = output["message"] + ("\n\n" + output["next_step"]["text"] if output["next_step"] else "")
            history.append({"role": "assistant", "text": shown})
        return outputs, state

    async def test_adult_who_agrees_gets_records_and_stage_without_model_calls(self):
        runner = FakeRunner()
        outputs, state = await self.converse(self.pipeline(runner, onboarding=True), ["你好", "1", "高中", "1"])
        self.assertIn("学习教练", outputs[0]["message"])
        self.assertIn("这次是谁在学", outputs[0]["next_step"]["text"])
        self.assertIn("哪个阶段", outputs[1]["next_step"]["text"])
        self.assertIn("要保存你的学习进度吗", outputs[2]["next_step"]["text"])
        self.assertIn("今天想弄懂点什么", outputs[3]["next_step"]["text"])
        self.assertEqual(runner.calls, [])
        self.assertTrue(self.store.is_enabled("s"))
        self.assertEqual(self.store.snapshot("s")["profile"]["learning_stage"]["value"], "高中")
        self.assertEqual(state["onboarding"]["records"], "on")

    async def test_first_question_is_answered_right_after_the_choices(self):
        runner = FakeRunner()
        outputs, state = await self.converse(self.pipeline(runner, onboarding=True),
                                             ["讲讲光合作用", "我自己", "5", "2"])
        self.assertIn("先记下", outputs[0]["message"])
        decide = runner.calls_for("coordinator", "decide")
        self.assertEqual(len(decide), 1)
        self.assertEqual(decide[0]["messages"], [{"turn": 1, "role": "user", "text": "讲讲光合作用"}])
        self.assertIs(decide[0]["first_meeting"], False)
        self.assertEqual(decide[0]["state"]["onboarding"]["stage"], "大学")
        self.assertTrue(outputs[3]["message"].startswith("好，这次不保存学习记录"))
        self.assertFalse(self.store.is_enabled("s"))

    async def test_child_alone_cannot_turn_records_on_until_a_guardian_confirms(self):
        runner = FakeRunner()
        pipeline = self.pipeline(runner, onboarding=True)
        outputs, state = await self.converse(pipeline, ["你好", "1", "小学", "不在", "开启记录", "1"])
        self.assertIn("家长现在在身边吗", outputs[2]["next_step"]["text"])
        self.assertIn("以后家长在身边时", outputs[3]["message"])
        self.assertIn("需要家长同意", outputs[4]["message"])
        self.assertIn("请家长确认", outputs[4]["next_step"]["text"])
        self.assertTrue(self.store.is_enabled("s"))
        self.assertEqual(state["onboarding"]["consent_by"], "guardian")

    async def test_companion_answers_for_the_child(self):
        outputs, state = await self.converse(self.pipeline(FakeRunner(), onboarding=True),
                                             ["你好", "2", "小学", "保存"])
        self.assertIn("孩子现在在哪个阶段", outputs[1]["next_step"]["text"])
        self.assertEqual(self.store.snapshot("s")["profile"]["learning_stage"]["origin"], "companion_report")

    async def test_unclear_choice_repeats_without_calling_a_model(self):
        runner = FakeRunner()
        outputs, state = await self.converse(self.pipeline(runner, onboarding=True), ["你好", "嗯"])
        self.assertIn("再选一次", outputs[1]["message"])
        self.assertIn("这次是谁在学", outputs[1]["next_step"]["text"])
        self.assertEqual(runner.calls, [])

    async def test_returning_visitor_and_default_pipeline_skip_the_questions(self):
        runner = FakeRunner()
        await self.pipeline(runner, onboarding=True).run(
            [{"role": "user", "text": "你好"}], session_id="r", returning_visitor=True)
        self.assertEqual(len(runner.calls_for("coordinator", "decide")), 1)
        plain = FakeRunner()
        await self.pipeline(plain).run([{"role": "user", "text": "你好"}], session_id="p")
        self.assertEqual(len(plain.calls_for("coordinator", "decide")), 1)

    async def test_demo_mode_skips_questions_with_preset_stage_and_records_on(self):
        runner = FakeRunner()
        _, state = await self.pipeline(runner, onboarding=True, demo_stage="高中").run(
            [{"role": "user", "text": "讲讲分数"}], session_id="d")
        decide = runner.calls_for("coordinator", "decide")[0]
        self.assertEqual(decide["state"]["onboarding"]["stage"], "高中")
        self.assertTrue(decide["memory_enabled"])
        self.assertTrue(state["onboarding"]["demo"])

    async def test_delete_needs_a_second_confirmation(self):
        self.store.set_enabled("x", True)
        pipeline = self.pipeline(FakeRunner())
        outputs, state = await self.converse(pipeline, ["删除记录", "确定删除"], session="x")
        self.assertIn("删除后找不回来", outputs[0]["message"])
        self.assertIn("已删除", outputs[1]["message"])
        self.assertFalse(self.store.is_enabled("x"))
        self.assertTrue(state["memory_blocked"])

    async def test_delete_request_lapses_if_the_learner_moves_on(self):
        runner = FakeRunner()
        self.store.set_enabled("y", True)
        outputs, state = await self.converse(self.pipeline(runner), ["删除记录", "讲讲分数", "确定删除"], session="y")
        self.assertTrue(self.store.is_enabled("y"))
        self.assertEqual(len(runner.calls_for("coordinator", "decide")), 2)


if __name__ == "__main__":
    unittest.main()
