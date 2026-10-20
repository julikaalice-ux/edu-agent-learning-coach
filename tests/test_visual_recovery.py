"""Topic-independent visual routing recovery, with refusal and policy boundaries."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "agent"
    ),
)
from learning_coach.domain.routing import recover_explicit_visual_route, select_tools
from learning_coach.domain.visual_quality import needs_explanatory_visual


def direct(primary="A", *, pause=False, end=False):
    return {
        "mode": "answer",
        "response": {
            "schema_version": "1.0",
            "intent": {"primary": primary, "is_implicit": False},
            "message": "先看看现象。",
            "next_step": {"kind": "question", "text": "你观察到了什么？"},
        },
        "task": {
            "task_summary": "理解现象的机制",
            "pause_task": pause,
            "end_task": end,
        },
    }


class GenericVisualRecoveryTests(unittest.TestCase):
    def test_explanatory_guard_covers_animation_simulation_and_sequential_changes(self):
        for text in ("请用动画说明月相变化", "模拟供需变化", "分步展示细胞分裂过程"):
            with self.subTest(text=text):
                self.assertTrue(needs_explanatory_visual(text, text))

    def test_explanatory_guard_keeps_positive_clause_after_a_decline(self):
        text = "不需要互动，但请用动画说明月相变化"
        self.assertTrue(needs_explanatory_visual("月相变化", text))
        self.assertTrue(recover_explicit_visual_route(direct(), text)[1])

    def test_explanatory_guard_does_not_override_definitions_exams_or_photo_optout(self):
        for text in ("模拟考试", "动画是什么意思", "不要演示，给我看实拍照片"):
            with self.subTest(text=text):
                self.assertFalse(needs_explanatory_visual("演示形成过程", text))

    def test_varied_topics_recover_visual_request_without_selecting_a_fixed_template(
        self,
    ):
        requests = [
            "演示一下丁达尔效应是如何形成的",
            "请演示牛顿第二定律",
            "模拟分子从高浓度区域扩散到低浓度区域的过程",
            "演示月相的变化",
            "请把神经冲动的传播过程演示出来",
            "用图解释供需变化",
            "用动画演示光合作用",
            "帮我做个演示讲清循环依赖",
            "画一张人体循环系统的结构图",
            "演示光合作用",
            "画个时间线，讲讲唐宋元明清的先后顺序",
            "画一下地球绕太阳转",
            "帮我画出串联电路",
        ]
        for text in requests:
            with self.subTest(text=text):
                original = direct()
                unchanged = deepcopy(original)
                result, repaired = recover_explicit_visual_route(original, text)
                self.assertTrue(repaired)
                self.assertEqual(original, unchanged)
                self.assertEqual(result["mode"], "tools")
                self.assertEqual(result["plan"]["visual_request"], text)
                # The answer already written is offered as the early explanation.
                self.assertEqual(result["initial_explanation"], original["response"]["message"])
                evidence = result["plan"]["tool_reasons"]["visual"]
                self.assertEqual(evidence["code"], "explicit_visual")
                self.assertIn(evidence["evidence_quote"], text)
                self.assertNotIn("simulation_id", result["plan"])
                _, tools = select_tools(result["plan"], text, memory_enabled=False)
                self.assertEqual(tools["visual"], "pending")
                self.assertTrue(
                    all(
                        value == "not_requested"
                        for key, value in tools.items()
                        if key != "visual"
                    )
                )

    def test_exam_slides_definitions_and_declined_visuals_remain_direct(self):
        messages = [
            "出一份丁达尔效应模拟考试",
            "请模拟考试的答题过程",
            "帮我模拟面试",
            "制作演示文稿",
            "我想了解动画是什么意思",
            "什么是演示",
            "请问什么叫模拟",
            "解释一下可视化的定义",
            "演示这个词是什么意思",
            "什么是图片",
            "不要演示丁达尔效应",
            "不需要演示牛顿第二定律",
            "不用图，文字解释光合作用",
            "不要再给我演示原理，只讲一下",
            "不需要用图解释",
            "不想看动画",
            "动画就不要了",
            "用文字说明这个模拟过程",
            "这个概念我还不太懂",
            "我画了一张画，你看看",
            "画家梵高是谁",
        ]
        for text in messages:
            with self.subTest(text=text):
                original = direct()
                result, repaired = recover_explicit_visual_route(original, text)
                self.assertFalse(repaired)
                self.assertIs(result, original)

    def test_declining_a_photo_does_not_cancel_an_explicit_animation_request(self):
        text = "不要照片，用动画解释月相变化"
        result, repaired = recover_explicit_visual_route(direct(), text)
        self.assertTrue(repaired)
        self.assertEqual(
            result["plan"]["tool_reasons"]["visual"]["evidence_quote"], "动画"
        )

    def test_emotion_safety_clarification_pause_and_end_take_precedence(self):
        decisions = [direct(primary) for primary in "EFG"] + [
            direct(pause=True),
            direct(end=True),
        ]
        for decision in decisions:
            with self.subTest(decision=decision):
                result, repaired = recover_explicit_visual_route(
                    decision, "演示光的传播过程"
                )
                self.assertFalse(repaired)
                self.assertIs(result, decision)

    def test_an_existing_tool_plan_is_not_rewritten(self):
        decision = {"mode": "tools", "plan": {"visual_request": "模型已选择的计划"}}
        result, repaired = recover_explicit_visual_route(decision, "用图解释分子扩散")
        self.assertFalse(repaired)
        self.assertIs(result, decision)


if __name__ == "__main__":
    unittest.main()
