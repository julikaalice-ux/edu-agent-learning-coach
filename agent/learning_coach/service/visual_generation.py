"""Generate, render-check and independently review a teaching visual before use."""

import asyncio
import base64
from copy import deepcopy

from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.teaching_visuals import visual_handoff
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.domain.visual_quality import (GALLERY_REVIEW_SCHEMA, VISUAL_REVIEW_SCHEMA, blocking_issues,
                                                  gallery_problem, needs_explanatory_visual, repair_gallery,
                                                  validate_gallery_review, validate_visual_review)
from learning_coach.service.execution import StageFailure, external_allowed
from learning_coach.service.ports import AgentRunner, Artifacts, ImageFinder, ImageGenerator, StorageError


_RENDER_CORRECTIONS = {
    "duplicate_node_id": "关系图的节点 id 重复，请为每个节点使用唯一 id。",
    "diagram_dangling_edge": "连线引用了不存在的节点；from/to 必须引用当前 nodes 中的 id。",
    "invalid_visual_range": "函数横坐标范围无效，必须 x_min < x_max。",
    "invalid_visual_number": "函数参数必须是协议范围内的有限数值。",
    "scene_duplicate_id": "同一帧存在重复元素 id，请保证每帧元素标识唯一。",
    "scene_nonfinite_number": "图形坐标、尺寸和透明度必须为有限数值。",
    "scene_zero_length_line": "线段或箭头没有长度，请给出不同的起点和终点。",
    "scene_out_of_bounds": "图形超出画布，位置加宽高后仍须落在0至100的范围内。",
    "scene_label_does_not_fit": "标签放不进它所属的图形，请缩短文字或扩大图形；小图形（如粒子、小球）把 label 留空，在旁边放一个 text 元素写名字。",
    "scene_label_out_of_bounds": "标签超出画布，请向内移动对象或缩短标签。",
    "scene_labels_overlap": ("标签相互遮挡，请调整布局或缩短文字。线和箭头的标签画在线段中点上方约 4 格、"
                             "宽约 19 格高约 14 格的范围里；从同一点出发的几支箭头标签容易叠在一起，"
                             "可以把箭头拉长、错开方向，或把 label 留空，改在不挡别的文字的地方放 text。"),
}


def _render_issue(error: ValueError, prefix: str = "") -> list[dict]:
    """The designer's correction for a render failure, with the frame and element ids when known."""
    message = prefix + _RENDER_CORRECTIONS.get(str(error), "草稿无法渲染；检查图形边界、元素标识、连线引用、坐标范围和数值条件。")
    where = getattr(error, "where", "")
    return _issue("render_invalid", message + (f"（出错位置：{where}）" if where else ""))


def _issue(code: str, message: str) -> list[dict]:
    return [{"code": code, "message": message}]


def unavailable_visual(code: str, issues: list[dict], attempts: int = 0) -> dict:
    return {"status": "unavailable", "description": "本轮未产出通过检查的教学图。",
            "failure": {"code": code, "issues": deepcopy(issues), "attempts": attempts}}


# Design, render check, review and one redraw together. A first draft and its
# review take 30-50 s; a redraw needs about as long again.
VISUAL_TIMEOUT_SECONDS = 120

# A drawn draft that rendered and was reviewed is kept as a fallback unless
# the review found one of these; it is published (with a fixed note) when no
# draft passes, so a learner who asked for a picture gets one. Wrong facts,
# missing evidence and a review that did not hold together never go out.
FALLBACK_BLOCKERS = frozenset({"factual_error", "insufficient_evidence", "review_inconsistent"})
FALLBACK_NOTE = "这张图审阅时还有没改完的地方（没有发现事实错误），细节以文字讲解为准。"

# At most this many candidate pictures go to the reviewer in one call.
MAX_REVIEW_IMAGES = 12
IMAGE_SEARCH_TIMEOUT = 20


async def find_pictures(finder: ImageFinder, spec: dict, cache: dict | None = None) -> list[list[dict]]:
    """Candidates for every gallery panel, searched in parallel; a failed panel gets [].

    cache (optional, per turn) keeps results by query, so a redraw that only
    fixed its text does not search the web again.
    """
    panels = spec["panels"]
    # This shared boundary also protects callers outside VisualGenerationService.
    # Cached candidates do not imply permission to use the external capability.
    if not external_allowed("image_search"):
        return [[] for _ in panels]
    per_panel = max(1, min(3, MAX_REVIEW_IMAGES // len(panels)))

    async def one(panel):
        key = (panel["query_zh"], panel["query_en"], per_panel)
        if cache is not None and key in cache:
            return cache[key]
        try:
            found = await asyncio.wait_for(finder.find(panel["query_zh"], panel["query_en"], limit=per_panel),
                                           timeout=IMAGE_SEARCH_TIMEOUT)
        except Exception:  # noqa: BLE001 - search is optional; a failed panel just has no pictures
            return []
        if cache is not None:
            cache[key] = found
        return found
    found = await asyncio.gather(*(one(panel) for panel in panels))
    return [list(options)[:per_panel] for options in found]


def photo_entry(option: dict) -> dict:
    """What the page shows for one chosen picture: the bytes as a data URL plus its credit."""
    return {"data_url": f"data:{option['media_type']};base64," + base64.b64encode(option["data"]).decode("ascii"),
            "title": option["title"][:200], "author": option["author"][:200],
            "license": option["license"][:80], "page_url": option["page_url"], "source": option["source"]}


class VisualGenerationService:
    """At most two drafts/reviews; publication belongs to the outer transaction.

    Program-drawn kinds are render-checked before review (render errors go
    back to the designer without spending a review). An AI picture is only
    generated after its specification passed review, so a rejected draft
    never costs an image call. A gallery's pictures are found by the program
    and chosen by the reviewer, who sees them, in the same review call.
    """

    def __init__(self, runner: AgentRunner, artifacts: Artifacts,
                 image_generator: ImageGenerator | None = None, *, image_finder: ImageFinder | None = None,
                 timeout: float = VISUAL_TIMEOUT_SECONDS):
        self.runner, self.artifacts, self.image_generator = runner, artifacts, image_generator
        self.image_finder = image_finder
        self.timeout = timeout
        self.drafts = 0

    async def generate(self, session_id: str, payload: dict, *, learner_text: str):
        # One deadline covers design, rendering, review and the optional redraw;
        # a slow optional visual must leave the main task able to return feedback.
        self.history, self._fallback, self._last_review = [], None, None
        try:
            artifact, context = await asyncio.wait_for(
                self._generate(session_id, payload, learner_text=learner_text), timeout=self.timeout)
        except TimeoutError:
            artifact, context = None, unavailable_visual("visual_timeout", _issue(
                "render_invalid", "图示生成与审阅超过本轮时间上限，未发布产物；可继续当前学习任务。"), self.drafts)
        if artifact is None and self._fallback is not None:
            # No draft passed (or time ran out): the best drawn one goes out with a note.
            artifact, context = self._fallback
            # Why the drafts were sent back, for logs and evaluations only.
            context["history"] = list(self.history)
        if artifact is None and context and "failure" in context:
            # Why each draft was sent back, for logs and evaluations (codes and review text only).
            context["failure"]["history"] = list(self.history)
        return artifact, context

    def _note(self, attempt, issues):
        self.history += [f"第 {attempt} 稿 {issue['code']}：{issue['message'][:160]}" for issue in issues[:3]]

    async def _generate(self, session_id: str, payload: dict, *, learner_text: str):
        research = payload.get("research")
        unverified = research is not None and research.get("overall") != "verified"
        if unverified:
            # Unverified material never reaches the designer or the reviewer:
            # the visual is drawn from stable basic knowledge only (the
            # designer's rule for research=null) and says so, instead of the
            # whole visual failing after the learner waited for the search.
            payload = {**payload, "research": None}
            research = None
        artifact, context = await self._attempts(session_id, payload, research, learner_text)
        if unverified and artifact is not None:
            context["limitations"].append("本轮检索资料没有核实成功，图里只画了稳定的基础知识，没有用检索到的说法。")
        return artifact, context

    async def _attempts(self, session_id, payload, research, learner_text):
        # Decided before drafting, so the designer is told up front instead of
        # having a whole draft rejected afterwards.
        explanatory = needs_explanatory_visual(payload["goal"], learner_text)
        requested_source = payload.get("visual_source") or "local"
        gallery_allowed = (requested_source == "web" and self.image_finder is not None
                           and external_allowed("image_search"))
        illustration_allowed = (requested_source == "generated" and self.image_generator is not None
                                and external_allowed("image_generation") and not explanatory)
        # The source directive and allowed kinds must agree. Unavailable or
        # unsuitable external modes become a local design before the first call.
        source = "web" if gallery_allowed else "generated" if illustration_allowed else "local"
        capabilities = {
            "image_search": {"available": self.image_finder is not None,
                             "authorized": self.image_finder is not None and external_allowed("image_search")},
            "image_generation": {"available": self.image_generator is not None,
                                 "authorized": self.image_generator is not None and external_allowed("image_generation")},
            "local_visual": {"available": self.artifacts is not None, "authorized": True},
        }
        self._found = {}  # pictures already found this turn, reused by a redraw
        previous_spec, issues = None, []
        for attempt in range(1, 3):
            self.drafts = attempt
            draft_input = deepcopy(payload)
            draft_input.update(attempt=attempt, visual_source=source, illustration_allowed=illustration_allowed,
                               gallery_allowed=gallery_allowed, capabilities=deepcopy(capabilities),
                               must_show_change=explanatory)
            if issues:
                draft_input.update(revision_issues=deepcopy(issues), previous_spec=previous_spec)
            try:
                spec = await self.runner("visualizer", draft_input, VISUAL_SCHEMA)
            except StageFailure:
                return None, unavailable_visual("generation_unavailable", _issue(
                    "schema_invalid", "图示设计服务本次未返回可用结果。"), attempt)
            try:
                validate(spec, VISUAL_SCHEMA)
            except ContractError:
                previous_spec = None
                issues = _issue("schema_invalid", "草稿未符合图示协议；请使用给定类型与字段，不输出网页代码或额外字段。")
                self._note(attempt, issues)
                continue
            previous_spec = deepcopy(spec)
            if spec["kind"] == "diagram" and explanatory:
                issues = _issue("interaction_problem", "这个请求要看变化或步骤，diagram 是静态图；请改用 scene（逐帧）或 process（分步）。")
                self._note(attempt, issues)
                continue
            if spec["kind"] == "illustration" and explanatory:
                issues = _issue("interaction_problem", "当前请求需要解释机制或演示变化；未经像素核验的静态 AI 配图不能完成此任务。请改用可检查的图解或分步 scene。")
                self._note(attempt, issues)
                continue
            if spec["kind"] == "illustration" and not illustration_allowed:
                issues = _issue("request_mismatch", "本轮未授权或未选择外部图片生成；请改用程序画的图。")
                self._note(attempt, issues)
                continue
            if spec["kind"] == "gallery":
                if not gallery_allowed:
                    issues = _issue("request_mismatch", "本轮不能使用网络图片（gallery）；请改用程序画的图。")
                    self._note(attempt, issues)
                    continue
                result = await self._gallery(session_id, payload, spec, research, learner_text, explanatory,
                                             attempt, issues)
            else:
                result = await self._drawn(session_id, payload, spec, research, learner_text, attempt, issues)
            outcome, value = result
            if outcome == "done":
                artifact, context = value
                if artifact is not None and spec["kind"] not in ("gallery", "illustration"):
                    if requested_source == "web":
                        context["limitations"].append("本轮未取得可用网络图片，当前展示的是本地图解。")
                    elif requested_source == "generated":
                        context["limitations"].append("本轮未取得可用 AI 配图，当前展示的是本地图解。")
                return value
            if outcome == "no_pictures":
                gallery_allowed = False
                source = "local"
            issues = value
            self._note(attempt, issues)
        return None, unavailable_visual("quality_not_accepted", issues, 2)

    async def _drawn(self, session_id, payload, spec, research, learner_text, attempt, previous_issues):
        """A program-drawn spec or an AI picture: render check, review, then (picture only) generate."""
        artifact = None
        if spec["kind"] == "illustration" and (
                payload.get("visual_source") != "generated" or not external_allowed("image_generation")):
            return "retry", _issue("request_mismatch", "外部图片生成未获本次授权；请改用程序画的图。")
        if spec["kind"] != "illustration":
            try:
                artifact = self.artifacts.prepare(session_id, spec)
            except ValueError as error:
                # Only translate known application codes, never expose provider
                # exceptions (which can carry prompts, paths or credentials).
                if str(error) == "invalid_visual_dependency":
                    return "done", (None, unavailable_visual("render_unavailable", _issue(
                        "render_invalid", "本地图形渲染依赖不可用，图示未发布。"), attempt))
                return "retry", _render_issue(error)
            except (RuntimeError, OSError, StorageError):
                return "done", (None, unavailable_visual("render_unavailable", _issue(
                    "render_invalid", "本次图示渲染服务不可用。"), attempt))
        review = await self._review(payload, spec, research, learner_text, attempt, previous_issues)
        if review[0] == "retry" and artifact is not None and self._last_review is not None:
            self._keep_fallback(spec, artifact, self._last_review, attempt)
        if review[0] != "pass":
            return review
        review = review[1]
        if spec["kind"] == "illustration":
            # Check again at the actual side-effect boundary; a model-supplied
            # spec or capability claim must never grant access to a backend.
            if payload.get("visual_source") != "generated" or not external_allowed("image_generation"):
                return "retry", _issue("request_mismatch", "外部图片生成未获本次授权；请改用程序画的图。")
            try:
                if self.image_generator is None:
                    raise RuntimeError("image_backend_unavailable")
                image = await self.image_generator(spec["prompt"])
                artifact = self.artifacts.prepare(session_id, spec, image=image)
            except Exception:  # noqa: BLE001 - provider details never reach logs or the learner
                return "done", (None, unavailable_visual("render_unavailable", _issue(
                    "render_invalid", "本次图片生成服务不可用。"), attempt))
        return "done", (artifact, self._accepted(spec, review, attempt))

    async def _review(self, payload, spec, research, learner_text, attempt, previous_issues,
                      *, candidates=None, images=None):
        """("pass", review), ("retry", issues) or ("done", unavailable result)."""
        self._last_review = None  # set again only by a complete review that sends the draft back
        request = {
            "phase": "visual_review", "goal": payload["goal"],
            "learning_goal": payload.get("learning_goal", ""),
            "learner_text": learner_text[:2000],
            "recent_messages": payload.get("recent_messages", []),
            "spec": spec, "research": research,
            "render_check": "passed", "review_scope": "specification_only",
            "pixel_reviewed": False,
            # A redraw is re-reviewed against the issues it was meant to fix.
            "attempt": attempt, "previous_issues": deepcopy(previous_issues),
        }
        if candidates is not None:
            request.update(candidates=candidates, review_scope="specification_and_candidate_pictures",
                           pixel_reviewed=True)
        try:
            if candidates is None:
                review = await self.runner("verifier", request, VISUAL_REVIEW_SCHEMA)
                validate_visual_review(review, research)
            else:
                review = await self.runner("verifier", request, GALLERY_REVIEW_SCHEMA, images=images)
                validate_gallery_review(review, research, [[option["id"] for option in panel["options"]]
                                                           for panel in candidates])
        except ContractError:
            return "retry", _issue("review_inconsistent", "审阅结果自相矛盾或声称使用了不存在的来源；请检查草稿事实与需求，下一轮如实审阅。")
        except StageFailure:
            return "done", (None, unavailable_visual("review_unavailable", _issue(
                "insufficient_evidence", "本轮独立审阅未完成，图示不发布。"), attempt))
        if review["verdict"] == "insufficient":
            return "done", (None, unavailable_visual("insufficient_evidence", deepcopy(review["issues"]), attempt))
        if review["verdict"] == "pass" or not blocking_issues(review):
            # Only minor issues: publish, with them shown as notes.
            return "pass", review
        self._last_review = review
        return "retry", deepcopy(review["issues"])

    def _keep_fallback(self, spec, artifact, review, attempt):
        """A rendered, reviewed draft without wrong facts; a later one replaces an earlier one."""
        if any(issue["code"] in FALLBACK_BLOCKERS for issue in blocking_issues(review)):
            return
        context = visual_handoff(spec)
        context.update(status="ready", quality={
            "verdict": review["verdict"], "fact_basis": review["fact_basis"],
            "review_scope": "specification_only", "pixel_reviewed": False,
            "attempts": attempt, "published_with_issues": True,
        })
        context["limitations"].append(FALLBACK_NOTE)
        self._fallback = (artifact, context)

    async def _gallery(self, session_id, payload, spec, research, learner_text, explanatory, attempt,
                       previous_issues):
        if payload.get("visual_source") != "web" or not external_allowed("image_search"):
            return "retry", _issue("request_mismatch", "联网搜图未获本次授权；请改用程序画的图。")
        # Small structure slips are fixed here rather than costing a whole redraw.
        spec = repair_gallery(spec)
        problem = gallery_problem(spec, explanatory)
        if problem:
            return "retry", problem
        try:
            # Render check without pictures: catches a broken companion before
            # any search or review is spent on this draft.
            self.artifacts.prepare(session_id, spec, photos=[None] * len(spec["panels"]))
        except ValueError as error:
            if str(error) == "invalid_visual_dependency":
                return "done", (None, unavailable_visual("render_unavailable", _issue(
                    "render_invalid", "本地图形渲染依赖不可用，图示未发布。"), attempt))
            return "retry", _render_issue(error, "配套图（companion）")
        except (RuntimeError, OSError, StorageError):
            return "done", (None, unavailable_visual("render_unavailable", _issue(
                "render_invalid", "本次图示渲染服务不可用。"), attempt))
        found = await find_pictures(self.image_finder, spec, cache=getattr(self, "_found", None))
        if not any(found):
            return "no_pictures", _issue("request_mismatch",
                "没有找到可用的网络图片；这一稿请不要再用 gallery，改用程序画的图（diagram、comparison、process 或 scene）。")
        candidates, images, pictures = [], [], {}
        for index, (panel, options) in enumerate(zip(spec["panels"], found)):
            listed = []
            for number, option in enumerate(options):
                key = f"p{index}c{number}"
                pictures[key] = option
                listed.append({"id": key, "source": option["source"], "title": option["title"][:120],
                               "description": option["description"][:300], "license": option["license"][:60]})
                images.append((f"候选图 {key}（第 {index} 格「{panel['label']}」）", option["data"], option["media_type"]))
            candidates.append({"panel": index, "label": panel["label"], "find": panel["find"], "options": listed})
        review = await self._review(payload, spec, research, learner_text, attempt, previous_issues,
                                    candidates=candidates, images=images)
        if review[0] != "pass":
            return review
        review = review[1]
        photos = [None] * len(spec["panels"])
        for choice in review["choices"]:
            option = pictures.get(choice["option"]) if choice["option"] else None
            if option is not None:
                photos[choice["panel"]] = photo_entry(option)
        try:
            artifact = self.artifacts.prepare(session_id, spec, photos=photos)
        except (ValueError, RuntimeError, OSError, StorageError):
            return "done", (None, unavailable_visual("render_unavailable", _issue(
                "render_invalid", "图片讲解页渲染失败，未发布。"), attempt))
        context = self._accepted(spec, review, attempt, pixel_reviewed=True)
        missing = [panel["label"] for panel, photo in zip(spec["panels"], photos) if photo is None]
        if missing:
            context["limitations"].append("这几格没有找到合适的图片，只显示文字要点：" + "、".join(missing))
        return "done", (artifact, context)

    @staticmethod
    def _accepted(spec, review, attempt, *, pixel_reviewed=False):
        context = visual_handoff(spec)
        scope = "specification_and_candidate_pictures" if pixel_reviewed else "specification_only"
        context.update(status="ready", quality={
            "verdict": "pass", "fact_basis": review["fact_basis"],
            "review_scope": scope, "pixel_reviewed": pixel_reviewed,
            "attempts": attempt,
        })
        if pixel_reviewed:
            context["limitations"].append("图片和文字经过独立模型审阅，这不等于正确性证明。")
        else:
            context["limitations"].append(
                "图示规格经过独立模型审阅与本地渲染检查，这不等于正确性证明或像素视觉核验。")
        if review["fact_basis"] == "general_knowledge":
            context["limitations"].append("事实审阅基于模型一般知识，本轮未对这些内容进行外部来源查证。")
        minor = [issue["message"] for issue in review["issues"] if issue.get("severity") == "minor"]
        if minor:
            context["limitations"].append("审阅提示（不影响使用）：" + "；".join(message[:160] for message in minor[:2]))
        return context
