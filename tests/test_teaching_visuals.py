"""Topic-independent visual generation, correction and quality gates."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import (FakeArtifacts, FakeResearch, FakeRunner, LearningPipeline,
                           LearningStore, RESPONSE_SCHEMA, VISUAL, messages, plan)
from test_direct_routing import direct_decision
from test_illustration import TINY_PNG
from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.teaching_visuals import visual_handoff
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.domain.visual_quality import REVIEW_CHECKS, validate_visual_review
from learning_coach.service.execution import StageFailure


def review(verdict="pass", *, basis="general_knowledge", code="factual_error", message="第二步的因果箭头方向反了，请从原因指向结果。"):
    return {"verdict": verdict, "checks": {key: verdict == "pass" for key in REVIEW_CHECKS},
            "issues": [] if verdict == "pass" else [{"code": code, "message": message}],
            "fact_basis": basis}


def scene(topic="光的散射"):
    return {"kind": "scene", "title": topic, "learning_goal": f"理解{topic}的形成过程",
            "interaction_hint": "选择步骤观察变化", "check_question": "第二步发生了什么变化？",
            "limitations": "定性分步示意，不按真实比例绘制。",
            "frames": [{"title": "第一步", "explanation": "先观察初始条件。", "elements": [
                {"id": "a", "shape": "rect", "x": 10, "y": 20, "width": 30, "height": 30,
                 "color": "neutral", "opacity": 1, "label": "对象"}]},
                {"title": "第二步", "explanation": "再观察条件变化后的结果。", "elements": [
                    {"id": "a", "shape": "rect", "x": 30, "y": 20, "width": 30, "height": 30,
                     "color": "accent", "opacity": 1, "label": "变化后的对象"}]}]}


class VisualReviewContractTests(unittest.TestCase):
    def test_contradictory_pass_is_rejected(self):
        cases = []
        failed = review(); failed["checks"]["factual_consistency"] = False; cases.append(failed)
        with_issue = review(); with_issue["issues"] = review("revise")["issues"]; cases.append(with_issue)
        missing_basis = review(basis="insufficient"); cases.append(missing_basis)
        unsupported_source = review(basis="provided_sources"); cases.append(unsupported_source)
        empty_revision = review("revise"); empty_revision["issues"] = []; cases.append(empty_revision)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ContractError):
                validate_visual_review(candidate, None)

    def test_general_knowledge_and_real_sources_remain_distinct(self):
        self.assertEqual(validate_visual_review(review(), None)["fact_basis"], "general_knowledge")
        verified = {"overall": "verified", "sources": [{"id": "s1", "excerpt": "source"}]}
        self.assertEqual(validate_visual_review(review(basis="provided_sources"), verified)["fact_basis"], "provided_sources")
        self.assertEqual(validate_visual_review(review("insufficient", basis="insufficient"), None)["verdict"], "insufficient")

    def test_scene_handoff_only_describes_implemented_controls(self):
        spec = scene()
        validate(spec, VISUAL_SCHEMA)
        context = visual_handoff(spec)
        self.assertEqual(context["kind"], "scene")
        self.assertEqual(context["observation_question"], spec["check_question"])
        self.assertIn("时间轴", "".join(context["controls"]))
        self.assertNotIn("浓度", "".join(context["controls"]))
        self.assertIn("不代表连续物理计算", "".join(context["limitations"]))


class TeachingVisualPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")
        self.research = FakeResearch()
        self.artifacts = FakeArtifacts()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, image_generator=None):
        return LearningPipeline(runner, self.research, self.store, self.artifacts, RESPONSE_SCHEMA,
                                image_generator=image_generator)

    async def test_different_topics_use_the_same_design_review_pipeline(self):
        for topic in ("丁达尔效应", "光合作用", "食物消化"):
            with self.subTest(topic=topic):
                text = f"演示一下{topic}的过程"
                runner = FakeRunner([plan(visual_request=text, task_summary=topic)], visuals=[scene(topic)])
                output, state = await self.pipeline(runner).run(messages(text), session_id=topic)
                self.assertEqual([call["role"] for call in runner.calls],
                                 ["coordinator", "visualizer", "verifier", "coordinator"])
                self.assertEqual(len(runner.calls_for("verifier", "visual_review")), 1)
                self.assertEqual(state["last_execution"]["tools"]["visual"], "published")
                context = runner.calls_for("coordinator", "respond")[0]["visual_context"]
                self.assertEqual(context["title"], topic)
                self.assertEqual(context["quality"]["fact_basis"], "general_knowledge")
                self.assertFalse(context["quality"]["pixel_reviewed"])
                self.assertIn("未对这些内容进行外部来源查证", "".join(context["limitations"]))
                self.assertNotIn("html", context)
                self.assertNotIn("base64", json.dumps(context))
                self.assertIn("打开互动图", output["message"])
        self.assertEqual(self.research.calls, [])

    async def test_revision_receives_specific_issues_and_only_second_draft_is_published(self):
        first, corrected = scene("初稿"), scene("修正后的因果图")
        runner = FakeRunner([plan(visual_request="演示形成过程")], visuals=[first, corrected],
                            visual_reviews=[review("revise"), review()])
        _, state = await self.pipeline(runner).run(messages("演示形成过程"), session_id="s")
        self.assertEqual(len(runner.calls_for("visualizer")), 2)
        self.assertEqual(len(runner.calls_for("verifier", "visual_review")), 2)
        retry = runner.calls_for("visualizer")[1]
        self.assertEqual(retry["revision_issues"], review("revise")["issues"])
        self.assertEqual(retry["previous_spec"], first)
        self.assertEqual(len(self.artifacts.prepared), 2)
        self.assertEqual(len(self.artifacts.committed), 1)
        self.assertEqual(self.artifacts.committed[0][1]["spec"]["title"], corrected["title"])
        self.assertEqual(state["last_execution"]["tools"]["visual"], "published")

    async def test_two_rejections_stop_without_publishing(self):
        runner = FakeRunner([plan(visual_request="展示关系")], visuals=[VISUAL, VISUAL],
                            visual_reviews=[review("revise"), review("revise")])
        output, state = await self.pipeline(runner).run(messages("请画图"), session_id="s")
        self.assertEqual(len(runner.calls), 6)
        self.assertEqual(self.artifacts.committed, [])
        self.assertIn("未生成可用", output["message"])
        context = runner.calls_for("coordinator", "respond")[0]["visual_context"]
        self.assertEqual(context["failure"]["attempts"], 2)
        self.assertEqual(context["failure"]["code"], "quality_not_accepted")
        self.assertEqual(state["last_execution"]["tools"]["visual"], "unavailable")

    async def test_insufficient_facts_fail_without_inventing_a_second_draft(self):
        runner = FakeRunner([plan(visual_request="解释未知反应")], visuals=[scene("未知反应")],
                            visual_reviews=[review("insufficient", basis="insufficient",
                                                   code="insufficient_evidence", message="缺少反应条件，无法确定产物和箭头。")])
        _, state = await self.pipeline(runner).run(messages("请画图说明未知反应"), session_id="s")
        self.assertEqual(len(runner.calls_for("visualizer")), 1)
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(state["last_execution"]["tools"]["visual"], "unavailable")
        self.assertEqual(runner.calls_for("coordinator", "respond")[0]["visual_context"]["failure"]["code"], "insufficient_evidence")

    async def test_inconsistent_review_cannot_publish_even_when_it_says_pass(self):
        bad = review(); bad["checks"]["labels_clear"] = False
        runner = FakeRunner([plan(visual_request="展示关系")], visuals=[VISUAL, VISUAL], visual_reviews=[bad, bad])
        await self.pipeline(runner).run(messages("请画图"), session_id="s")
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(runner.calls_for("visualizer")[1]["revision_issues"][0]["code"], "review_inconsistent")

    async def test_fabricated_external_source_basis_cannot_publish(self):
        bad = review(basis="provided_sources")
        runner = FakeRunner([plan(visual_request="展示关系")], visuals=[VISUAL, VISUAL], visual_reviews=[bad, bad])
        await self.pipeline(runner).run(messages("请画图"), session_id="s")
        self.assertEqual(self.artifacts.committed, [])

    async def test_illustration_is_replaced_for_an_explicit_mechanism_demo(self):
        picture = {"kind": "illustration", "title": "漂亮画面", "prompt": "a science picture", "alt": "演示画面"}
        runner = FakeRunner([plan(visual_request="演示光合作用形成过程")], visuals=[picture, scene("光合作用")])
        async def forbidden_image_call(prompt):
            raise AssertionError("Do not render a bitmap that cannot explain this mechanism")
        await self.pipeline(runner, forbidden_image_call).run(messages("演示光合作用形成过程"), session_id="s")
        self.assertEqual(len(runner.calls_for("verifier", "visual_review")), 1)
        self.assertEqual(self.artifacts.committed[0][1]["spec"]["kind"], "scene")
        self.assertEqual(runner.calls_for("visualizer")[1]["revision_issues"][0]["code"], "interaction_problem")

    async def test_appearance_image_is_allowed_but_its_pixels_are_not_claimed_verified(self):
        class ImageArtifacts(FakeArtifacts):
            def prepare(self, session_id, spec, image=None):
                self.image = image
                return super().prepare(session_id, spec)
        self.artifacts = ImageArtifacts()
        picture = {"kind": "illustration", "title": "松树外观", "prompt": "a pine tree", "alt": "松树的外观配图"}
        runner = FakeRunner([plan(visual_request="松树外观配图")], visuals=[picture])
        async def image_generator(prompt):
            return TINY_PNG
        await self.pipeline(runner, image_generator).run(messages("给我松树外观配图"), session_id="s")
        self.assertEqual(self.artifacts.image, TINY_PNG)
        self.assertEqual(len(self.artifacts.committed), 1)
        review_input = runner.calls_for("verifier", "visual_review")[0]
        self.assertFalse(review_input["pixel_reviewed"])
        self.assertEqual(review_input["review_scope"], "specification_only")
        self.assertNotIn("image", review_input)
        context = runner.calls_for("coordinator", "respond")[0]["visual_context"]
        self.assertIn("像素视觉核验", "".join(context["limitations"]))

    async def test_invalid_schema_is_corrected_before_review(self):
        runner = FakeRunner([plan(visual_request="展示关系")],
                            visuals=[{"kind": "html", "code": "PRIVATE_SCRIPT"}, VISUAL])
        output, _ = await self.pipeline(runner).run(messages("请画图"), session_id="s")
        self.assertEqual(len(runner.calls_for("verifier", "visual_review")), 1)
        self.assertEqual(len(self.artifacts.prepared), 1)
        self.assertEqual(len(self.artifacts.committed), 1)
        retry = runner.calls_for("visualizer")[1]
        self.assertIsNone(retry["previous_spec"])
        self.assertEqual(retry["revision_issues"][0]["code"], "schema_invalid")
        self.assertNotIn("PRIVATE_SCRIPT", output["message"])

    async def test_renderer_rejects_draft_before_independent_review(self):
        class RejectFirst(FakeArtifacts):
            failed = False
            def prepare(self, session_id, spec):
                if not self.failed:
                    self.failed = True
                    raise ValueError("diagram_dangling_edge")
                return super().prepare(session_id, spec)
        self.artifacts = RejectFirst()
        runner = FakeRunner([plan(visual_request="展示关系")], visuals=[VISUAL, VISUAL])
        await self.pipeline(runner).run(messages("请画图"), session_id="s")
        self.assertEqual(len(runner.calls_for("verifier", "visual_review")), 1)
        self.assertEqual(runner.calls_for("visualizer")[1]["revision_issues"][0]["code"], "render_invalid")
        self.assertIn("from/to", runner.calls_for("visualizer")[1]["revision_issues"][0]["message"])
        self.assertEqual(len(self.artifacts.committed), 1)

    async def test_visualizer_and_reviewer_receive_bounded_context(self):
        runner = FakeRunner([plan(visual_request="对比它们", task_summary="理解分子与分母")], visuals=[VISUAL])
        history = [{"role": "user" if i % 2 == 0 else "assistant", "text": str(i) + "长" * 4000}
                   for i in range(8)]
        await self.pipeline(runner).run(messages("画一张比较图", *history), session_id="s")
        design = runner.calls_for("visualizer")[0]
        checking = runner.calls_for("verifier", "visual_review")[0]
        self.assertEqual(design["learning_goal"], "理解分子与分母")
        self.assertEqual(len(design["recent_messages"]), 4)
        self.assertTrue(all(len(item["text"]) <= 1500 for item in design["recent_messages"]))
        self.assertEqual(checking["recent_messages"], design["recent_messages"])
        self.assertEqual(checking["spec"], VISUAL)
        self.assertEqual(checking["render_check"], "passed")

    async def test_verifier_outage_does_not_publish_unreviewed_artifact(self):
        runner = FakeRunner([plan(visual_request="展示关系")], visuals=[VISUAL],
                            visual_reviews=[StageFailure("PRIVATE_PROVIDER")])
        output, _ = await self.pipeline(runner).run(messages("请画图"), session_id="s")
        self.assertEqual(len(self.artifacts.prepared), 1)
        self.assertEqual(self.artifacts.committed, [])
        self.assertNotIn("PRIVATE_PROVIDER", output["message"])
        self.assertEqual(runner.calls_for("coordinator", "respond")[0]["visual_context"]["failure"]["code"], "review_unavailable")

    async def test_unverified_research_blocks_design_and_review(self):
        self.research = FakeResearch({"overall": "likely", "sources": [], "claims": []})
        runner = FakeRunner([plan(visual_request="解释资料", research_query="查证资料")])
        _, state = await self.pipeline(runner).run(messages("查证后画图"), session_id="s")
        self.assertEqual(runner.calls_for("visualizer"), [])
        self.assertEqual(runner.calls_for("verifier"), [])
        self.assertEqual(self.artifacts.prepared, [])
        self.assertEqual(state["last_execution"]["tools"]["visual"], "unavailable")
        self.assertEqual(runner.calls_for("coordinator", "respond")[0]["visual_context"]["failure"]["code"], "research_unverified")

    async def test_non_visual_answer_still_finishes_in_one_call(self):
        runner = FakeRunner([direct_decision()])
        await self.pipeline(runner).run(messages("请解释这个概念"), session_id="s")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.artifacts.prepared, [])


if __name__ == "__main__":
    unittest.main()
