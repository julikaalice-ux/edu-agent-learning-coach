"""Learning-note data contract; independent of storage implementations."""

from typing import Any

SCHEMA_VERSION = "1.0"
# The four error types of the product design, plus "not confirmed yet".
ERROR_CATEGORIES = ("conceptual", "step_skipping", "misreading", "transfer_failure", "unclassified")
ERROR_TYPES = ERROR_CATEGORIES[:4]
ERROR_LABELS = {"conceptual": "概念性误解", "step_skipping": "步骤性跳跃", "misreading": "审题偏差",
                "transfer_failure": "迁移失败", "unclassified": "原因待确认"}
# Earlier stored names and the type each now counts as.
LEGACY_ERROR_CATEGORIES = {"procedural": "step_skipping", "careless": "misreading",
                           "negative_transfer": "conceptual"}
# Who a profile entry came from: the learner, or a companion speaking for them.
SELF_REPORT, COMPANION_REPORT = "self_report", "companion_report"
PROFILE_KEYS = (
    "learning_stage", "age_band", "learning_goal", "prior_knowledge",
    "preferred_language", "available_time", "accessibility_needs",
    "companion_role", "preferred_method", "learning_context",
)


def _string(limit: int = 1000) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": limit, "pattern": r"\S"}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items, "maxItems": 30}


NOTES_SCHEMA = _object({
    "schema_version": {"type": "string", "const": SCHEMA_VERSION},
    "profile": _array(_object({
        "key": {"type": "string", "enum": list(PROFILE_KEYS)},
        "value": _string(),
        "origin": {"type": "string", "enum": [SELF_REPORT, COMPANION_REPORT]},
        "evidence_quote": _string(4000),
    })),
    "concepts": _array(_object({
        "concept_id": _string(200),
        "label": _string(200),
        "prerequisites": {"anyOf": [{"type": "null"}, _array(_string(200))]},
        "related": {"anyOf": [{"type": "null"}, _array(_string(200))]},
        "evidence_quote": _string(4000),
    })),
    "error_records": _array(_object({
        "concept_id": _string(200),
        "category": {"type": "string", "enum": list(ERROR_CATEGORIES)},
        "cause_confirmed": {"type": "boolean"},
        "evidence_quote": _string(4000),
    })),
    "review_plan": _array(_object({
        "topic": _string(200),
        "deadline": {"anyOf": [{"type": "null"}, _string(100)]},
        "evidence_quote": _string(4000),
    })),
})

