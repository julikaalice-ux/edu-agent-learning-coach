"""The program derives execution order from declared content dependencies."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from learning_coach.domain.routing import execution_order  # noqa: E402


def plan(research=None, visual=None, quiz="none", **deps):
    value = {"research_query": research, "visual_request": visual, "quiz_action": quiz,
             "visual_uses_research": None, "quiz_uses_research": None, "quiz_uses_visual": None}
    value.update(deps)
    return value


class ExecutionOrderTests(unittest.TestCase):
    def test_without_research_everything_runs_together(self):
        self.assertEqual(execution_order(plan(visual="图", quiz="start")), {
            "visual_waits_research": False, "quiz_waits_research": False, "quiz_waits_visual": False})

    def test_unstated_research_dependency_is_treated_as_real(self):
        order = execution_order(plan(research="q", visual="图", quiz="start"))
        self.assertTrue(order["visual_waits_research"])
        self.assertTrue(order["quiz_waits_research"])

    def test_explicitly_unrelated_work_does_not_wait_for_research(self):
        order = execution_order(plan(research="q", visual="图", quiz="start",
                                     visual_uses_research=False, quiz_uses_research=False))
        self.assertFalse(order["visual_waits_research"])
        self.assertFalse(order["quiz_waits_research"])

    def test_question_waits_for_picture_only_when_stated(self):
        self.assertFalse(execution_order(plan(visual="图", quiz="start"))["quiz_waits_visual"])
        self.assertTrue(execution_order(plan(visual="图", quiz="start", quiz_uses_visual=True))["quiz_waits_visual"])

    def test_wait_on_work_that_does_not_run_is_dropped(self):
        order = execution_order(plan(quiz="start", quiz_uses_visual=True, quiz_uses_research=True))
        self.assertEqual(set(order.values()), {False})
        self.assertFalse(execution_order(plan(research="q", visual_uses_research=True))["visual_waits_research"])


if __name__ == "__main__":
    unittest.main()
