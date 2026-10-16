"""Consistency rules for an independent, bounded visual specification review."""

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


def validate_visual_review(review: dict, research: dict | None) -> dict:
    """A declared pass cannot override failed checks or missing evidence."""
    validate(review, VISUAL_REVIEW_SCHEMA)
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
