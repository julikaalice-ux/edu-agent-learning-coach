"""Consistency rules for an independent, bounded visual specification review."""

from copy import deepcopy
import re

from learning_coach.domain.contracts import ContractError, object_schema, validate
from learning_coach.domain.routing import visual_request_clauses


REVIEW_CHECKS = ("request_match", "factual_consistency", "labels_clear",
                 "interaction_useful", "not_misleading")
ISSUE_CODES = ("request_mismatch", "factual_error", "label_problem", "interaction_problem",
               "misleading", "insufficient_evidence", "schema_invalid", "render_invalid",
               "review_inconsistent")
ISSUE_SCHEMA = object_schema({
    "code": {"enum": list(ISSUE_CODES)},
    "message": {"type": "string", "minLength": 1, "maxLength": 500, "pattern": r"\S"},
})
VISUAL_REVIEW_SCHEMA = object_schema({
    "verdict": {"enum": ["pass", "revise", "insufficient"]},
    "checks": object_schema({key: {"type": "boolean"} for key in REVIEW_CHECKS}),
    "issues": {"type": "array", "maxItems": 6, "items": ISSUE_SCHEMA},
    "fact_basis": {"enum": ["provided_sources", "general_knowledge", "insufficient"]},
})


GALLERY_REVIEW_SCHEMA = object_schema({
    **VISUAL_REVIEW_SCHEMA["properties"],
    # One entry per panel: the candidate picture the reviewer picked, or null.
    "choices": {"type": "array", "maxItems": 6, "items": object_schema({
        "panel": {"type": "integer", "minimum": 0, "maximum": 5},
        "option": {"type": ["string", "null"], "maxLength": 20},
    })},
})
# Kinds that can explain a mechanism or a change; a picture page asked to
# explain one must carry one of these as its companion.
EXPLANATORY_KINDS = ("scene", "process", "diagram", "linear_function")


def validate_visual_review(review: dict, research: dict | None) -> dict:
    """A declared pass cannot override failed checks or missing evidence."""
    validate(review, VISUAL_REVIEW_SCHEMA)
    return _consistent(review, research)


def validate_gallery_review(review: dict, research: dict | None, options: list[list[str]]) -> dict:
    """Same consistency rules, plus exactly one choice per panel from that panel's own candidates."""
    validate(review, GALLERY_REVIEW_SCHEMA)
    _consistent(review, research)
    chosen = {}
    for choice in review["choices"]:
        panel, option = choice["panel"], choice["option"]
        if panel >= len(options) or panel in chosen:
            raise ContractError("gallery_review_bad_panel")
        if option is not None and option not in options[panel]:
            raise ContractError("gallery_review_unknown_candidate")
        chosen[panel] = option
    if len(chosen) != len(options):
        raise ContractError("gallery_review_missing_panel")
    if review["verdict"] == "pass" and not any(chosen.values()):
        raise ContractError("gallery_review_pass_without_picture")
    return review


def repair_gallery(spec: dict) -> dict:
    """Fix slips that need no new content: a step pointing past the last panel
    points at no panel; a timeline missing a year falls back to the automatic layout."""
    fixed = deepcopy(spec)
    for step in fixed["steps"]:
        if step["panel"] is not None and step["panel"] >= len(fixed["panels"]):
            step["panel"] = None
    if fixed["arrange"] == "timeline" and any(not (panel["time"] or "").strip() for panel in fixed["panels"]):
        fixed["arrange"] = "auto"
    return fixed


def gallery_problem(spec: dict, explanatory: bool) -> list[dict]:
    """Structure rules the schema cannot express; [] when the page is consistent."""
    panels = spec["panels"]
    for number, step in enumerate(spec["steps"], 1):
        if step["panel"] is not None and step["panel"] >= len(panels):
            return [{"code": "schema_invalid",
                     "message": f"讲解第 {number} 步指向第 {step['panel'] + 1} 格，但只有 {len(panels)} 格；panel 从 0 开始编号。"}]
    if spec["arrange"] == "timeline" and any(not (panel["time"] or "").strip() for panel in panels):
        return [{"code": "schema_invalid", "message": "时间轴排法要求每一格都填 time（年份或时期）；没有时间就改用 auto。"}]
    companion = spec["companion"]
    if explanatory and (companion is None or companion["kind"] not in EXPLANATORY_KINDS):
        return [{"code": "interaction_problem",
                 "message": "学习者要看变化、过程或原理；网络图片只能说明外观。请在 companion 放一张程序画的 scene、process、diagram 或 linear_function。"}]
    return []


def _consistent(review: dict, research: dict | None) -> dict:
    if review["verdict"] == "pass":
        if not all(review["checks"].values()) or review["issues"] or review["fact_basis"] == "insufficient":
            raise ContractError("visual_review_inconsistent")
    elif not review["issues"]:
        raise ContractError("visual_review_missing_issues")
    if (review["verdict"] == "insufficient") != (review["fact_basis"] == "insufficient"):
        raise ContractError("visual_review_inconsistent_basis")
    if review["fact_basis"] == "provided_sources" and not (
            research and research.get("overall") == "verified" and research.get("sources")):
        raise ContractError("visual_review_missing_sources")
    return review


def needs_explanatory_visual(goal: str, learner_text: str) -> bool:
    """Conservative purpose guard; it never selects a topic-specific template.

    The independent reviewer also checks purpose using the full request. This
    local check only prevents an obvious demonstration request from accepting
    an uninspected bitmap as evidence of a mechanism.
    """
    purpose = re.compile(r"演示|互动|交互|动画|动图|模拟|分步|逐帧|原理|机制|因果|"
                         r"如何形成|形成过程|变化过程|传播过程|运动过程|"
                         r"demonstrat|interactiv|animat|simulat|step.by.step|mechanism|how .{0,30}works")
    english_decline = re.compile(r"(?:do not|don't|no need to)\s+(?:\w+\s+){0,3}(?:demonstrat|interact|animat|simulat)")
    def matches(text):
        return any(purpose.search(clause) and not english_decline.search(clause)
                   for clause in visual_request_clauses(text.lower()))
    if matches(learner_text):
        return True
    # A current opt-out or an explicit appearance request must override a stale
    # coordinator goal; a positive animation clause above still wins.
    if (re.search(r"不用|不要|无需|不需要|不想|别|取消|停止|照片|实拍|外观|photograph|photoreal", learner_text.lower())
            or english_decline.search(learner_text.lower())
            or not list(visual_request_clauses(learner_text))):
        return False
    return matches(goal)
