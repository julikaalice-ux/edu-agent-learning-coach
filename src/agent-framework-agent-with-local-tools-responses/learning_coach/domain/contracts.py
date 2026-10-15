"""Internal contracts are separate from the public four-field response."""

import json
import math

from jsonschema import Draft202012Validator


class ContractError(ValueError):
    """A fixed diagnostic, with no learner/model content in the error."""


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"}
OPTIONAL_TEXT = {"anyOf": [TEXT, {"type": "null"}]}
OPTIONAL_BOOL = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
DEPENDENCY_FIELDS = ("visual_uses_research", "quiz_uses_research", "quiz_uses_visual")
INTENT_SCHEMA = object_schema({
    "primary": {"enum": list("ABCDEFG")}, "is_implicit": {"type": "boolean"},
})
TASK_SCHEMA = object_schema({
    "task_summary": OPTIONAL_TEXT,
    "pause_task": {"type": "boolean"},
    "end_task": {"type": "boolean"},
})
TOOL_REASON_CODES = {
    "research": ("explicit_lookup", "time_sensitive", "fact_check", "knowledge_gap"),
    "quiz": ("explicit_practice", "accepted_check", "continue_practice"),
    "notes": ("profile_update", "learning_evidence", "error_record", "review_plan"),
    "visual": ("explicit_visual", "comparison", "process", "function", "active_recall"),
}
TOOL_REASONS_SCHEMA = object_schema({
    name: {"anyOf": [
        {"type": "null"},
        object_schema({"code": {"type": "string", "enum": list(codes)}, "evidence_quote": TEXT}),
    ]}
    for name, codes in TOOL_REASON_CODES.items()
})
PLAN_SCHEMA = object_schema({
    "intent": INTENT_SCHEMA,
    "research_query": OPTIONAL_TEXT,
    "visual_request": OPTIONAL_TEXT,
    "observe_learning": {"type": "boolean"},
    "task_summary": OPTIONAL_TEXT,
    "pause_task": {"type": "boolean"},
    "end_task": {"type": "boolean"},
    "quiz_action": {"enum": ["none", "start", "continue", "stop"]},
    "quiz_topic": OPTIONAL_TEXT,
    "quiz_total": {"type": "integer", "minimum": 1, "maximum": 20},
    "difficulty": {"enum": ["简单", "中等", "困难"]},
    "participant": {"enum": ["learner", "companion_report", "unknown"]},
    "tool_reasons": TOOL_REASONS_SCHEMA,
    # Content dependencies only; the program derives the execution order.
    # null means "not stated" and gets the program's safe default.
    "visual_uses_research": OPTIONAL_BOOL,
    "quiz_uses_research": OPTIONAL_BOOL,
    "quiz_uses_visual": OPTIONAL_BOOL,
})


def decision_schema(response_schema):
    """Choose one complete public answer or one bounded internal tool plan."""
    return {"anyOf": [
        object_schema({
            "mode": {"type": "string", "const": "answer"},
            "response": response_schema,
            "task": TASK_SCHEMA,
        }),
        object_schema({
            "mode": {"type": "string", "const": "tools"},
            "plan": PLAN_SCHEMA,
        }),
    ]}


QUIZ_SCHEMA = object_schema({
    "concept_id": TEXT,
    "question": TEXT,
    "choices": {"type": "array", "items": TEXT, "minItems": 2, "maxItems": 4, "uniqueItems": True},
    "answer_index": {"type": "integer", "minimum": 0, "maximum": 3},
    "explanation": TEXT,
})


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ContractError("duplicate_key")
        result[key] = value
    return result


def _constant(_value):
    raise ContractError("non_finite_number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ContractError("non_finite_number")
    return result


def validate(value, schema):
    if not Draft202012Validator(schema).is_valid(value):
        raise ContractError("schema_mismatch")
    return value


def parse(text, schema):
    if not isinstance(text, str) or len(text) > 100_000:
        raise ContractError("invalid_response_size")
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_float)
    except (ValueError, RecursionError) as error:
        raise ContractError("invalid_json") from None
    return validate(value, schema)


def public_quiz(quiz):
    """Do not send the answer key to the agent composing an unanswered question."""
    return {key: quiz[key] for key in ("concept_id", "question", "choices")}


def grade_choice(text, quiz):
    """Only unambiguous choices get programmatic grading, never guessed prose."""
    value = text.strip()
    letters = "ABCD"[:len(quiz["choices"])]
    if value.upper() in letters and len(value) == 1:
        selected = letters.index(value.upper())
    elif value in quiz["choices"]:
        selected = quiz["choices"].index(value)
    else:
        return None
    return selected == quiz["answer_index"]
