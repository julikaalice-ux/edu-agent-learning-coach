"""Web-picture gallery pages, and the faster visual flow around them."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import (FakeArtifacts, FakeResearch, FakeRunner, LearningPipeline,
                           LearningStore, RESPONSE_SCHEMA, VISUAL, messages, plan)
from test_illustration import TINY_PNG
from test_teaching_visuals import review, scene
from learning_coach.adapters import image_search
from learning_coach.adapters.runner import ROLES, StructuredRunner
from learning_coach.adapters.visuals import render_visual
from learning_coach.db.artifacts import ArtifactStore
from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.teaching_visuals import gallery_arrangement, visual_handoff
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.domain.visual_quality import gallery_problem, validate_gallery_review
from learning_coach.service.execution import EXTERNAL_PERMISSIONS, StageFailure
from learning_coach.service.pipeline import CHECK_OFFER
from learning_coach.service.visual_generation import VisualGenerationService


@contextmanager
def authorized_images(capability="image_search"):
    token = EXTERNAL_PERMISSIONS.set(frozenset({capability}))
    try:
        yield
    finally:
        EXTERNAL_PERMISSIONS.reset(token)


def panel(label, *, time=None):
    return {"label": label, "find": f"{label}的显微照片或清楚的示意图", "query_zh": f"{label} 显微",
            "query_en": f"{label} micrograph", "time": time, "points": [f"{label}的一个特点"]}


def gallery(*labels, arrange="auto", steps=(), companion=None, times=None):
    return {"kind": "gallery", "title": "细胞长什么样", "arrange": arrange,
            "panels": [panel(label, time=(times or {}).get(label)) for label in labels],
            "steps": list(steps), "companion": companion}


def picture(source="commons", title="Plant cell", data=TINY_PNG):
    return {"source": source, "title": title, "description": "a plant cell", "author": "Someone",
            "license": "CC BY-SA 4.0" if source == "commons" else "",
            "page_url": "https://commons.wikimedia.org/wiki/File:Plant_cell.png",
            "data": data, "media_type": "image/png"}


def gallery_review(choices, verdict="pass"):
    return {**review(verdict), "choices": [{"panel": index, "option": option} for index, option in enumerate(choices)]}


class FakeFinder:
    def __init__(self, results):
        self.results = deque_of(results)
        self.calls = []

    async def find(self, query_zh, query_en, *, limit=3):
        self.calls.append((query_zh, query_en, limit))
        found = self.results.pop(0) if self.results else []
        if isinstance(found, Exception):
            raise found
        return found[:limit]


def deque_of(items):
    return list(items)


class GalleryRunner(FakeRunner):
    """FakeRunner that also accepts pictures, records them and answers gallery reviews."""

    def __init__(self, *args, gallery_reviews=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.gallery_reviews = list(gallery_reviews)
        self.images = []

    async def __call__(self, role, payload, schema, images=None):
        if images is not None:
            self.calls.append({"role": role, "payload": deepcopy(payload), "schema": deepcopy(schema)})
            self.images.append(images)
            return deepcopy(self.gallery_reviews.pop(0))
        return await super().__call__(role, payload, schema)


class GalleryContractTests(unittest.TestCase):
    def test_gallery_validates_and_companion_is_limited_to_drawn_kinds(self):
        validate(gallery("植物细胞", "动物细胞", companion=VISUAL), VISUAL_SCHEMA)
        for companion in (gallery("x"), {"kind": "illustration", "title": "画", "prompt": "a cell", "alt": "细胞"},
                          {"kind": "flashcards", "title": "卡", "cards": [{"front": "问", "back": "答"}]}):
            with self.subTest(kind=companion["kind"]), self.assertRaises(ContractError):
                validate(gallery("植物细胞", companion=companion), VISUAL_SCHEMA)
        with self.assertRaises(ContractError):
            validate(gallery(*[f"格{i}" for i in range(7)]), VISUAL_SCHEMA)

    def test_structure_rules_the_schema_cannot_express(self):
        self.assertEqual(gallery_problem(gallery("a", "b"), False), [])
        bad_step = gallery("a", steps=[{"panel": 3, "text": "看第四格"}])
        self.assertIn("只有 1 格", gallery_problem(bad_step, False)[0]["message"])
        timeline = gallery("甲骨", "竹简", "纸", arrange="timeline", times={"甲骨": "商代", "竹简": "战国"})
        self.assertIn("time", gallery_problem(timeline, False)[0]["message"])
        self.assertEqual(gallery_problem(gallery("a", companion=VISUAL), True)[0]["code"], "interaction_problem")
        self.assertEqual(gallery_problem(gallery("a", companion=scene()), True), [])

    def test_small_slips_are_repaired_without_a_redraw(self):
        from learning_coach.domain.visual_quality import repair_gallery
        slipped = gallery("甲骨", "纸", arrange="timeline", times={"甲骨": "商代"},
                          steps=[{"panel": 4, "text": "看第五格"}])
        fixed = repair_gallery(slipped)
        self.assertEqual(fixed["arrange"], "auto")
        self.assertIsNone(fixed["steps"][0]["panel"])
        self.assertEqual(gallery_problem(fixed, False), [])
        self.assertEqual(slipped["arrange"], "timeline")  # the draft itself is left as it was

    def test_arrangement_resolves_from_content(self):
        self.assertEqual(gallery_arrangement(gallery("a")), "stack")
        self.assertEqual(gallery_arrangement(gallery("a", "b")), "side_by_side")
        times = {"甲骨": "商代", "竹简": "战国", "纸": "东汉"}
        self.assertEqual(gallery_arrangement(gallery("甲骨", "竹简", "纸", times=times)), "timeline")
        self.assertEqual(gallery_arrangement(gallery("a", "b", "c", "d")), "tabs")
        self.assertEqual(gallery_arrangement(gallery("a", "b", arrange="stack")), "stack")

    def test_review_must_choose_once_per_panel_from_that_panels_candidates(self):
        options = [["p0c0", "p0c1"], ["p1c0"]]
        validate_gallery_review(gallery_review(["p0c1", None]), None, options)
        for bad, error in ((gallery_review(["p1c0", None]), "unknown_candidate"),
                           (gallery_review(["p0c0"]), "missing_panel"),
                           (gallery_review([None, None]), "pass_without_picture")):
            with self.subTest(error=error), self.assertRaisesRegex(ContractError, error):
                validate_gallery_review(bad, None, options)

    def test_handoff_describes_controls_and_the_companion(self):
        context = visual_handoff(gallery("甲骨", "竹简", "纸", times={"甲骨": "商代", "竹简": "战国", "纸": "东汉"},
                                         steps=[{"panel": 0, "text": "先看甲骨"}], companion=scene()))
        self.assertIn("播放或暂停时间轴", context["controls"])
        self.assertIn("下一步（分步讲解，会高亮对应的格）", context["controls"])
        self.assertIn("选择步骤", context["controls"])
        self.assertEqual(context["companion_kind"], "scene")
        self.assertIn("不是原理证据", "".join(context["limitations"]))


class GalleryRenderTests(unittest.TestCase):
    def photo(self, **changes):
        return {"data_url": "data:image/png;base64,iVBORw0KGgo=", "title": "Plant <cell>", "author": "A",
                "license": "CC BY-SA 4.0", "page_url": "https://commons.wikimedia.org/wiki/File:X.png",
                "source": "commons", **changes}

    def test_page_embeds_pictures_credits_and_escapes_model_text(self):
        spec = gallery("植物<b>细胞", "动物细胞", steps=[{"panel": 1, "text": "再看动物细胞"}])
        page = render_visual(spec, photos=[self.photo(), None])
        self.assertIn('src="data:image/png;base64,iVBORw0KGgo="', page)
        self.assertIn("植物&lt;b&gt;细胞", page)
        self.assertNotIn("<b>细胞", page)
        self.assertIn("Plant &lt;cell&gt;", page)
        self.assertIn("许可证 CC BY-SA 4.0", page)
        self.assertIn("这一格没有找到合适的网络图片", page)
        self.assertIn('data-panel="1"', page)
        self.assertIn("img-src data:", page)

    def test_companion_is_embedded_and_its_scripts_allowed_by_hash(self):
        page = render_visual(gallery("植物细胞", companion=scene()), photos=[self.photo()])
        self.assertIn('<iframe title="光的散射" sandbox="allow-scripts" srcdoc="', page)
        policy = page.split('Content-Security-Policy" content="')[1].split('"')[0]
        self.assertGreaterEqual(policy.count("sha256-"), 3)  # page + svg.js + scene script

    def test_unsafe_picture_data_and_links_are_rejected_or_dropped(self):
        with self.assertRaisesRegex(ValueError, "gallery_invalid_photo"):
            render_visual(gallery("a"), photos=[self.photo(data_url="javascript:alert(1)")])
        page = render_visual(gallery("a"), photos=[self.photo(page_url="javascript:alert(1)")])
        self.assertNotIn("javascript:", page)
        with self.assertRaisesRegex(ValueError, "photo_count"):
            render_visual(gallery("a", "b"), photos=[self.photo()])


class GalleryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.artifacts = ArtifactStore(Path(self.temp.name) / "artifacts", renderer=render_visual)
        # These tests exercise already-authorized provider/review behavior.
        self.permission = EXTERNAL_PERMISSIONS.set(frozenset({"image_search"}))

    def tearDown(self):
        EXTERNAL_PERMISSIONS.reset(self.permission)
        self.temp.cleanup()

    def service(self, runner, finder):
        return VisualGenerationService(runner, self.artifacts, image_finder=finder)

    def payload(self, goal="看看植物细胞和动物细胞长什么样"):
        return {"goal": goal, "research": None, "recent_messages": [], "visual_source": "web"}

    async def test_reviewer_sees_candidates_and_picks_one_per_panel(self):
        finder = FakeFinder([[picture(), picture("bocha", "Plant cell 2")], [picture(title="Animal cell")]])
        runner = GalleryRunner(visuals=[gallery("植物细胞", "动物细胞", companion=VISUAL)],
                               gallery_reviews=[gallery_review(["p0c1", None])])
        artifact, context = await self.service(runner, finder).generate("s", self.payload(),
                                                                         learner_text="找图片看看细胞")
        self.assertIsNotNone(artifact)
        labels = [label for label, _, _ in runner.images[0]]
        self.assertEqual(len(labels), 3)
        self.assertIn("p0c1", labels[1])
        request = runner.calls_for("verifier", "visual_review")[0]
        self.assertTrue(request["pixel_reviewed"])
        self.assertEqual([option["id"] for option in request["candidates"][0]["options"]], ["p0c0", "p0c1"])
        self.assertNotIn("data", json.dumps(request["candidates"]))
        self.assertIn("网页搜索", artifact["html"])  # the chosen picture came from BoCha
        self.assertIn("动物细胞", "".join(context["limitations"]))  # no picture for the second panel
        self.assertTrue(context["quality"]["pixel_reviewed"])
        self.assertEqual(runner.calls_for("visualizer")[0]["gallery_allowed"], True)

    async def test_no_pictures_found_turns_galleries_off_for_the_redraw(self):
        finder = FakeFinder([[], OSError("down")])
        runner = GalleryRunner(visuals=[gallery("植物细胞", "动物细胞"), VISUAL])
        artifact, context = await self.service(runner, finder).generate("s", self.payload(),
                                                                         learner_text="找图片看看细胞")
        self.assertIsNotNone(artifact)
        redraw = runner.calls_for("visualizer")[1]
        self.assertFalse(redraw["gallery_allowed"])
        self.assertEqual(redraw["revision_issues"][0]["code"], "request_mismatch")
        self.assertEqual(context["kind"], "comparison")

    async def test_without_a_finder_a_gallery_draft_is_sent_back(self):
        runner = GalleryRunner(visuals=[gallery("植物细胞"), VISUAL])
        artifact, _ = await self.service(runner, None).generate("s", self.payload(), learner_text="看细胞")
        self.assertIsNotNone(artifact)
        self.assertFalse(runner.calls_for("visualizer")[0]["gallery_allowed"])

    async def test_mechanism_request_needs_a_drawn_companion(self):
        finder = FakeFinder([[picture()]])
        runner = GalleryRunner(visuals=[gallery("叶绿体"), gallery("叶绿体", companion=scene("光合作用"))],
                               gallery_reviews=[gallery_review(["p0c0"])])
        artifact, context = await self.service(runner, finder).generate(
            "s", self.payload("演示光合作用的过程"), learner_text="演示光合作用的过程")
        self.assertIsNotNone(artifact)
        first = runner.calls_for("visualizer")[0]
        self.assertFalse(first["illustration_allowed"])
        self.assertEqual(runner.calls_for("visualizer")[1]["revision_issues"][0]["code"], "interaction_problem")
        self.assertEqual(context["companion_kind"], "scene")


class FasterVisualFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_picture_request_is_flagged_to_the_coordinator(self):
        temp = tempfile.TemporaryDirectory()
        store = LearningStore(Path(temp.name) / "notes.sqlite3")
        try:
            for text, flagged in (("画一个结构图展示三角形的分类", True), ("三角形怎么分类？", False)):
                runner = FakeRunner([plan()])
                await LearningPipeline(runner, FakeResearch(), store, FakeArtifacts(), RESPONSE_SCHEMA).run(
                    messages(text), session_id=text)
                with self.subTest(text=text):
                    self.assertEqual(runner.calls_for("coordinator", "decide")[0].get("visual_required", False),
                                     flagged)
        finally:
            store.close()
            temp.cleanup()

    async def test_a_redraw_reuses_the_pictures_already_found(self):
        temp = tempfile.TemporaryDirectory()
        try:
            finder = FakeFinder([[picture()], [picture()]])
            runner = GalleryRunner(visuals=[gallery("植物细胞"), gallery("植物细胞")],
                                   gallery_reviews=[gallery_review(["p0c0"], verdict="revise"),
                                                    gallery_review(["p0c0"])])
            artifacts = ArtifactStore(Path(temp.name) / "artifacts", renderer=render_visual)
            with authorized_images():
                artifact, _ = await VisualGenerationService(runner, artifacts, image_finder=finder).generate(
                    "s", {"goal": "看看植物细胞", "research": None, "visual_source": "web"}, learner_text="看看植物细胞")
            self.assertIsNotNone(artifact)
            self.assertEqual(len(finder.calls), 1)
        finally:
            temp.cleanup()

    async def test_a_failed_visual_keeps_why_each_draft_was_sent_back(self):
        runner = FakeRunner(visuals=[VISUAL, VISUAL], visual_reviews=[review("revise", message="第一稿的问题"),
                                                                      review("revise", message="第二稿的问题")])
        artifact, context = await VisualGenerationService(runner, FakeArtifacts()).generate(
            "s", {"goal": "对照", "research": None}, learner_text="画个对比图")
        self.assertIsNone(artifact)
        history = context["failure"]["history"]
        self.assertEqual(len(history), 2)
        self.assertIn("第 1 稿", history[0])
        self.assertIn("第二稿的问题", history[1])

    async def test_ai_picture_is_generated_only_after_its_review_passes(self):
        picture_spec = {"kind": "illustration", "title": "超市", "prompt": "a supermarket", "alt": "超市"}
        calls = []

        async def generate(prompt):
            calls.append(prompt)
            return TINY_PNG

        class ImageArtifacts(FakeArtifacts):
            def prepare(self, session_id, spec, image=None):
                return super().prepare(session_id, spec)
        runner = FakeRunner(visuals=[picture_spec, picture_spec], visual_reviews=[review("revise"), review()])
        with authorized_images("image_generation"):
            artifact, _ = await VisualGenerationService(runner, ImageArtifacts(), generate).generate(
                "s", {"goal": "超市的画面", "research": None, "visual_source": "generated"}, learner_text="画一个超市")
        self.assertIsNotNone(artifact)
        self.assertEqual(len(calls), 1)  # the rejected first draft cost no picture

    async def test_visual_observation_is_preserved_and_missing_step_can_offer_check(self):
        temp = tempfile.TemporaryDirectory()
        store = LearningStore(Path(temp.name) / "notes.sqlite3")
        try:
            cases = ((scene("水的三态"), {"kind": "question", "text": "第二步与第一步相比发生了什么变化？"}),
                     (VISUAL, {"kind": "task", "text": "对照图中的两列，找出分子与分母分别表示什么。"}),
                     (VISUAL, None))
            for index, (visual, next_step) in enumerate(cases):
                decision = {"mode": "tools", "plan": plan(visual_request="画图讲水的三态"),
                            "check_question": {"topic": "水的三态", "difficulty": "简单"}}
                decision["plan"]["tool_reasons"] = {"research": None, "quiz": None, "notes": None,
                                                    "visual": {"code": "explicit_visual", "evidence_quote": "画图"}}
                response = {**FakeRunner.default_reply(decision["plan"]["intent"]), "next_step": next_step}
                runner = FakeRunner([decision], visuals=[visual], replies=[response])
                pipeline = LearningPipeline(runner, FakeResearch(), store, FakeArtifacts(), RESPONSE_SCHEMA)
                output, _ = await pipeline.run(messages("画图讲水的三态"), session_id=f"t{index}")
                with self.subTest(kind=visual["kind"]):
                    self.assertEqual(runner.calls_for("quizzer"), [])  # a check is only offered, never asked here
                    offered = (output["next_step"] or {}).get("text") == CHECK_OFFER
                    self.assertEqual(offered, next_step is None)
                    if next_step is not None:
                        self.assertEqual(output["next_step"], next_step)
        finally:
            store.close()
            temp.cleanup()


class ReviewSeverityTests(unittest.IsolatedAsyncioTestCase):
    async def test_minor_issues_publish_with_a_note_and_must_fix_issues_redraw(self):
        minor = {**review("revise", message="第三步还可以拆成两步。"),
                 "issues": [{"code": "label_problem", "message": "第三步还可以拆成两步。", "severity": "minor"}]}
        for verdict in ("pass", "revise"):
            with self.subTest(verdict=verdict):
                given = {**minor, "verdict": verdict}
                runner = FakeRunner(visuals=[VISUAL], visual_reviews=[given])
                artifact, context = await VisualGenerationService(runner, FakeArtifacts()).generate(
                    "s", {"goal": "对照", "research": None}, learner_text="画个对比图")
                self.assertIsNotNone(artifact)
                self.assertEqual(len(runner.calls_for("visualizer")), 1)
                self.assertIn("第三步还可以拆成两步", "".join(context["limitations"]))
        runner = FakeRunner(visuals=[VISUAL, VISUAL], visual_reviews=[review("revise"), review()])
        artifact, _ = await VisualGenerationService(runner, FakeArtifacts()).generate(
            "s", {"goal": "对照", "research": None}, learner_text="画个对比图")
        self.assertIsNotNone(artifact)
        self.assertEqual(len(runner.calls_for("visualizer")), 2)

    def test_a_pass_cannot_hide_a_must_fix_issue(self):
        from learning_coach.domain.visual_quality import validate_visual_review
        hidden = {**review(), "issues": review("revise")["issues"]}
        with self.assertRaises(ContractError):
            validate_visual_review(hidden, None)

    def test_schema_problem_names_the_path_not_the_value(self):
        from learning_coach.domain.contracts import schema_problem
        broken = json.dumps({**gallery("甲骨"), "arrange": "SECRET_VALUE"}, ensure_ascii=False)
        where = schema_problem(broken, VISUAL_SCHEMA)
        self.assertNotIn("SECRET_VALUE", where)
        self.assertEqual(schema_problem("not json", VISUAL_SCHEMA), "invalid_json")

    def test_flashcard_requests_count_as_picture_requests(self):
        from learning_coach.domain.routing import explicit_visual_quote
        self.assertEqual(explicit_visual_quote("给我做一组元素周期表前 20 号元素的记忆卡片"), "记忆卡")
        self.assertIsNone(explicit_visual_quote("弹出测试卡片"))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pictures_go_into_one_message_with_their_labels(self):
        seen = []

        class Agent:
            async def run(self, message, options=None):
                seen.append(message)

                class Response:
                    text = json.dumps({"ok": True})
                    usage_details = {}
                return Response()
        runner = StructuredRunner(dict.fromkeys(ROLES, Agent()), retry_delay=0)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                  "additionalProperties": False}
        await runner("verifier", {"phase": "visual_review"}, schema, images=[("候选图 p0c0", TINY_PNG, "image/png")])
        contents = seen[0].contents
        self.assertEqual([content.type for content in contents], ["text", "text", "data"])
        self.assertEqual(contents[1].text, "候选图 p0c0")
        self.assertEqual(runner.usage[0]["images"], 1)

    async def test_a_timed_out_drawing_is_not_started_again(self):
        calls = []

        class Slow:
            async def run(self, message, options=None):
                calls.append(1)
                await asyncio.sleep(1)
        runner = StructuredRunner(dict.fromkeys(ROLES, Slow()), retry_delay=0, timeout=0.01,
                                  role_timeouts={"visualizer": 0.01})
        with self.assertRaises(StageFailure):
            await runner("visualizer", {}, {"type": "object"})
        self.assertEqual(len(calls), 1)
        calls.clear()
        with self.assertRaises(StageFailure):
            await runner("coordinator", {}, {"type": "object"})
        self.assertEqual(len(calls), 2)  # other roles keep their one retry


class ImageSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_only_reusable_licenses_pass(self):
        for name, allowed in (("CC BY-SA 4.0", True), ("Public domain", True), ("CC0", True),
                              ("CC BY-NC 2.0", False), ("CC BY-ND 4.0", False), ("", False), ("GFDL", False)):
            with self.subTest(name=name):
                self.assertEqual(image_search.license_allowed(name), allowed)

    def test_commons_results_keep_rank_license_and_picture_types(self):
        response = {"query": {"pages": [
            {"index": 2, "title": "File:B.jpg", "imageinfo": [{
                "mime": "image/jpeg", "thumburl": "https://upload.wikimedia.org/b.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:B.jpg",
                "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "Artist": {"value": "<a href='x'>Bob</a>"},
                                "ImageDescription": {"value": "<p>Animal&nbsp;cell</p>"}}}]},
            {"index": 1, "title": "File:A.png", "imageinfo": [{
                "mime": "image/png", "thumburl": "https://upload.wikimedia.org/a.png",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:A.png",
                "extmetadata": {"LicenseShortName": {"value": "Public domain"}}}]},
            {"index": 3, "title": "File:C.pdf", "imageinfo": [{
                "mime": "application/pdf", "thumburl": "https://upload.wikimedia.org/c.jpg",
                "extmetadata": {"LicenseShortName": {"value": "CC0"}}}]},
            {"index": 4, "title": "File:D.jpg", "imageinfo": [{
                "mime": "image/jpeg", "thumburl": "https://upload.wikimedia.org/d.jpg",
                "extmetadata": {"LicenseShortName": {"value": "CC BY-NC 2.0"}}}]},
        ]}}
        with mock.patch.object(image_search, "_json", return_value=response):
            found = image_search.commons_candidates("cell", 3)
        self.assertEqual([item["title"] for item in found], ["A.png", "B.jpg"])
        self.assertEqual(found[1]["author"], "Bob")
        self.assertEqual(found[1]["description"], "Animal cell")

    def test_bocha_image_results_are_read_from_the_bing_shape(self):
        response = {"code": 200, "data": {"images": {"value": [
            {"name": "细胞", "contentUrl": "https://example.com/a.jpg", "hostPageUrl": "https://example.com/a",
             "thumbnailUrl": "https://example.com/t.jpg"},
            {"name": "不安全", "contentUrl": "http://example.com/b.jpg"}]}}}
        with mock.patch.object(image_search, "_json", return_value=response):
            found = image_search.bocha_candidates("key", "细胞", 3)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["page_url"], "https://example.com/a")

    def test_downloads_only_from_public_https_addresses(self):
        for url in ("http://example.com/a.png", "https://user:pw@example.com/a.png", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(image_search.RetrievalError):
                image_search.download_image(url)
        self.assertFalse(image_search._public_host("localhost"))
        with self.assertRaisesRegex(image_search.RetrievalError, "host_rejected"):
            image_search.download_image("https://127.0.0.1/a.png")

    async def test_nothing_found_retries_once_with_the_core_words(self):
        queries = []

        def commons(query, limit):
            queries.append(query)
            return [] if len(queries) == 1 else [{"source": "commons", "title": "c", "description": "", "author": "",
                                                  "license": "CC0", "page_url": "", "url": "https://upload.wikimedia.org/c"}]
        with mock.patch.object(image_search, "commons_candidates", side_effect=commons), \
                mock.patch.object(image_search, "download_image", return_value=(TINY_PNG, "image/png")):
            found = await image_search.WebImageFinder().find("植物细胞", "plant cell under light microscope 400x")
        self.assertEqual(queries, ["plant cell under light microscope 400x", "plant cell light"])
        self.assertEqual(len(found), 1)
        self.assertEqual(image_search.simpler_query("plant cell"), "")

    async def test_the_finder_reports_what_happened_for_the_evaluation(self):
        with mock.patch.object(image_search, "commons_candidates", return_value=[]):
            stats = {}
            found = await image_search.WebImageFinder().find("植物细胞", "plant cell under light microscope",
                                                             stats=stats)
        self.assertEqual(found, [])
        self.assertEqual((stats["listed"], stats["downloaded"], stats["retried"]), (0, 0, "plant cell light"))

    async def test_an_unreachable_source_is_skipped_for_a_while(self):
        calls = []

        def bocha(*args):
            calls.append(1)
            raise OSError("unreachable")
        with mock.patch.object(image_search, "commons_candidates", return_value=[]), \
                mock.patch.object(image_search, "bocha_candidates", side_effect=bocha):
            finder = image_search.WebImageFinder("key")
            await finder.find("细胞", "cell")
            await finder.find("细胞", "cell")
        self.assertEqual(len(calls), 1)

    async def test_commons_pictures_come_before_bocha_and_failed_downloads_are_skipped(self):
        commons = [{"source": "commons", "title": t, "description": "", "author": "", "license": "CC0",
                    "page_url": "", "url": f"https://upload.wikimedia.org/{t}"} for t in ("c1", "c2")]
        bocha = [{"source": "bocha", "title": "b1", "description": "", "author": "", "license": "",
                  "page_url": "", "url": "https://example.com/b1", "fallback_url": ""}]

        def download(url):
            if url.endswith("c2"):
                raise image_search.RetrievalError("not_a_picture")
            return TINY_PNG, "image/png"
        with mock.patch.object(image_search, "commons_candidates", return_value=commons), \
                mock.patch.object(image_search, "bocha_candidates", return_value=bocha), \
                mock.patch.object(image_search, "download_image", side_effect=download):
            found = await image_search.WebImageFinder("key").find("细胞", "cell", limit=2)
        self.assertEqual([item["title"] for item in found], ["c1", "b1"])
        self.assertEqual(found[0]["data"], TINY_PNG)
        self.assertNotIn("url", found[0])


class FluxTokenTests(unittest.TestCase):
    def test_one_credential_and_one_token_for_many_pictures(self):
        from learning_coach.adapters import illustrate

        class Token:
            token, expires_on = "t", 10 ** 12

        built = []

        class Credential:
            def __init__(self):
                built.append(self)
                self.calls = 0

            def get_token(self, scope, **kwargs):
                self.calls += 1
                return Token()
        from learning_coach.adapters import scope_credential

        class Unavailable:
            def __init__(self, *args):
                pass

            def get_token(self, scope, **kwargs):
                from azure.identity import CredentialUnavailableError
                raise CredentialUnavailableError("no az cli in tests")

        with mock.patch.object(scope_credential, "ScopeCliCredential", Unavailable), \
                mock.patch("azure.identity.DefaultAzureCredential", Credential), \
                mock.patch.object(illustrate, "_CREDENTIAL", None), mock.patch.object(illustrate, "_TOKEN", None):
            self.assertEqual([illustrate._flux_token() for _ in range(3)], ["t", "t", "t"])
            self.assertEqual(len(built), 1)
            self.assertEqual(built[0].calls, 1)


class SharedCredentialTests(unittest.TestCase):
    def test_pictures_use_the_apps_credential_when_given(self):
        from learning_coach.adapters import illustrate

        class Token:
            token, expires_on = "shared", 10 ** 12

        class Credential:
            def get_token(self, scope):
                return Token()

        with mock.patch.object(illustrate, "_CREDENTIAL", None), mock.patch.object(illustrate, "_TOKEN", None):
            illustrate.use_credential(Credential())
            self.assertEqual(illustrate._flux_token(), "shared")


class TimingReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_picture_steps_are_reported_to_the_stage_observer(self):
        from learning_coach.server import _timed_pictures
        records = []

        async def generate(prompt):
            return TINY_PNG
        generate_timed, finder_timed = _timed_pictures(generate, FakeFinder([[picture()]]), records.append)
        await generate_timed("a cat")
        await finder_timed.find("猫", "cat", limit=2)
        self.assertEqual([(record["role"], record["outcome"]) for record in records],
                         [("image", "ok"), ("image_search", "ok")])
        self.assertEqual(records[1]["results"], 1)
        self.assertNotIn("prompt", json.dumps(records))


if __name__ == "__main__":
    unittest.main()
