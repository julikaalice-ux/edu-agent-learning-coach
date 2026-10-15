"""Generate, render-check and independently review a teaching visual before use."""

import asyncio
from copy import deepcopy

from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.teaching_visuals import visual_handoff
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.domain.visual_quality import (VISUAL_REVIEW_SCHEMA,
                                                  needs_explanatory_visual,
                                                  validate_visual_review)
from learning_coach.service.execution import StageFailure
from learning_coach.service.ports import AgentRunner, Artifacts, ImageGenerator, StorageError


_RENDER_CORRECTIONS = {
    "duplicate_node_id": "关系图的节点 id 重复，请为每个节点使用唯一 id。",
    "diagram_dangling_edge": "连线引用了不存在的节点；from/to 必须引用当前 nodes 中的 id。",
    "invalid_visual_range": "函数横坐标范围无效，必须 x_min < x_max。",
    "invalid_visual_number": "函数参数必须是协议范围内的有限数值。",
    "scene_duplicate_id": "同一帧存在重复元素 id，请保证每帧元素标识唯一。",
    "scene_nonfinite_number": "图形坐标、尺寸和透明度必须为有限数值。",
    "scene_zero_length_line": "线段或箭头没有长度，请给出不同的起点和终点。",
    "scene_out_of_bounds": "图形超出画布，位置加宽高后仍须落在0至100的范围内。",
    "scene_label_does_not_fit": "标签超过可用空间，请缩短文字或扩大所属图形。",
    "scene_label_out_of_bounds": "标签超出画布，请向内移动对象或缩短标签。",
    "scene_labels_overlap": "标签相互遮挡，请调整布局或缩短文字。",
}


def _issue(code: str, message: str) -> list[dict]:
    return [{"code": code, "message": message}]


def unavailable_visual(code: str, issues: list[dict], attempts: int = 0) -> dict:
    return {"status": "unavailable", "description": "本轮未产出通过检查的教学图。",
            "failure": {"code": code, "issues": deepcopy(issues), "attempts": attempts}}


class VisualGenerationService:
    """At most two drafts/reviews; publication belongs to the outer transaction."""

    def __init__(self, runner: AgentRunner, artifacts: Artifacts,
                 image_generator: ImageGenerator | None = None, *, timeout: float = 60):
        self.runner, self.artifacts, self.image_generator = runner, artifacts, image_generator
        self.timeout = timeout

    async def generate(self, session_id: str, payload: dict, *, learner_text: str):
        # One deadline covers design, rendering, review and the optional redraw;
        # a slow optional visual must leave the main task able to return feedback.
        try:
            return await asyncio.wait_for(
                self._generate(session_id, payload, learner_text=learner_text), timeout=self.timeout)
        except TimeoutError:
            return None, unavailable_visual("visual_timeout", _issue(
                "render_invalid", "图示生成与审阅超过本轮时间上限，未发布产物；可继续当前学习任务。"))

    async def _generate(self, session_id: str, payload: dict, *, learner_text: str):
        research = payload.get("research")
        if research is not None and research.get("overall") != "verified":
            return None, unavailable_visual("research_unverified", _issue(
                "insufficient_evidence", "当前检索资料未完成核实，不能据此绘制确定结论。"))
        previous_spec, issues = None, []
        for attempt in range(1, 3):
            draft_input = deepcopy(payload)
            draft_input.update(attempt=attempt)
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
                continue
            previous_spec = deepcopy(spec)
            if spec["kind"] == "illustration" and needs_explanatory_visual(payload["goal"], learner_text):
                issues = _issue("interaction_problem", "当前请求需要解释机制或演示变化；未经像素核验的静态 AI 配图不能完成此任务。请改用可检查的图解或分步 scene。")
                continue
            try:
                image = None
                if spec["kind"] == "illustration":
                    if self.image_generator is None:
                        raise RuntimeError("image_backend_unavailable")
                    image = await self.image_generator(spec["prompt"])
                artifact = (self.artifacts.prepare(session_id, spec, image=image) if image is not None
                            else self.artifacts.prepare(session_id, spec))
            except ValueError as error:
                # Only translate known application codes, never expose provider
                # exceptions (which can carry prompts, paths or credentials).
                if str(error) == "invalid_visual_dependency":
                    return None, unavailable_visual("render_unavailable", _issue(
                        "render_invalid", "本地图形渲染依赖不可用，图示未发布。"), attempt)
                issues = _issue("render_invalid", _RENDER_CORRECTIONS.get(str(error),
                    "草稿无法渲染；检查图形边界、元素标识、连线引用、坐标范围和数值条件。"))
                continue
            except (RuntimeError, OSError, StorageError):
                return None, unavailable_visual("render_unavailable", _issue(
                    "render_invalid", "本次图示渲染或图片生成服务不可用。"), attempt)
            try:
                review = await self.runner("verifier", {
                    "phase": "visual_review", "goal": payload["goal"],
                    "learning_goal": payload.get("learning_goal", ""),
                    "learner_text": learner_text[:2000],
                    "recent_messages": payload.get("recent_messages", []),
                    "spec": spec, "research": research,
                    "render_check": "passed", "review_scope": "specification_only",
                    "pixel_reviewed": False,
                    # A redraw is re-reviewed against the issues it was meant to fix.
                    "attempt": attempt, "previous_issues": deepcopy(issues),
                }, VISUAL_REVIEW_SCHEMA)
                validate_visual_review(review, research)
            except ContractError:
                issues = _issue("review_inconsistent", "审阅结果自相矛盾或声称使用了不存在的来源；请检查草稿事实与需求，下一轮如实审阅。")
                continue
            except StageFailure:
                return None, unavailable_visual("review_unavailable", _issue(
                    "insufficient_evidence", "本轮独立审阅未完成，图示不发布。"), attempt)
            if review["verdict"] == "pass":
                context = visual_handoff(spec)
                context.update(status="ready", quality={
                    "verdict": "pass", "fact_basis": review["fact_basis"],
                    "review_scope": "specification_only", "pixel_reviewed": False,
                    "attempts": attempt,
                })
                context["limitations"].append(
                    "图示规格经过独立模型审阅与本地渲染检查，这不等于正确性证明或像素视觉核验。")
                if review["fact_basis"] == "general_knowledge":
                    context["limitations"].append("事实审阅基于模型一般知识，本轮未对这些内容进行外部来源查证。")
                return artifact, context
            issues = deepcopy(review["issues"])
            if review["verdict"] == "insufficient":
                return None, unavailable_visual("insufficient_evidence", issues, attempt)
        return None, unavailable_visual("quality_not_accepted", issues, 2)
