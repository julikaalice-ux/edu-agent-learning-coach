"""Internal contracts are separate from the public four-field response."""

from enum import StrEnum
import json
import math

from jsonschema import Draft202012Validator

from learning_coach.domain.context import CONTEXT_UPDATE_SCHEMA
from learning_coach.domain.note_contracts import ERROR_TYPES
from learning_coach.domain.research_contracts import MAX_QUERIES, MAX_QUERY_CHARS


class ContractError(ValueError):
    """A fixed diagnostic, with no learner/model content in the error."""


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"}
# Largest raw model reply accepted for JSON parsing.
MAX_RESPONSE_CHARS = 100_000
# Longest learner question carried between stages (onboarding, research, pipeline).
MAX_QUESTION_CHARS = 4000
OPTIONAL_TEXT = {"anyOf": [TEXT, {"type": "null"}]}
EASY = "简单"
DIFFICULTY = {"enum": [EASY, "中等", "困难"]}


class QuizAction(StrEnum):
    NONE = "none"
    START = "start"
    CONTINUE = "continue"
    STOP = "stop"


class Participant(StrEnum):
    """Who answers: the learner alone, a companion reporting for them, or not known."""
    LEARNER = "learner"
    COMPANION = "companion_report"
    UNKNOWN = "unknown"


class VisualSource(StrEnum):
    LOCAL = "local"            # drawn by the program
    WEB = "web"                # existing pictures found on the web
    GENERATED = "generated"    # an AI picture


class QuizKind(StrEnum):
    PRACTICE = "practice"      # a set the learner asked for
    CHECK = "check"            # one question on the explanation just given


class DecisionMode(StrEnum):
    ANSWER = "answer"          # the coordinator replies directly
    TOOLS = "tools"            # the coordinator asks for tools first


def values(names) -> list:
    """The plain strings of a StrEnum, for schemas."""
    return [member.value for member in names]
OPTIONAL_BOOL = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
# The coordinator writes the search queries itself when it asks for research.
SEARCH_QUERIES = {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 1, "maxItems": MAX_QUERIES,
    "items": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS, "pattern": r"\S"}}]}
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
    "visual": ("explicit_visual", "comparison", "process", "structure", "function", "active_recall"),
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
    "search_queries": SEARCH_QUERIES,
    "visual_request": OPTIONAL_TEXT,
    "visual_source": {"enum": [None, *values(VisualSource)]},
    "observe_learning": {"type": "boolean"},
    "task_summary": OPTIONAL_TEXT,
    "pause_task": {"type": "boolean"},
    "end_task": {"type": "boolean"},
    "quiz_action": {"enum": values(QuizAction)},
    "quiz_topic": OPTIONAL_TEXT,
    "quiz_total": {"type": "integer", "minimum": 1, "maximum": 20},
    "difficulty": DIFFICULTY,
    "participant": {"enum": values(Participant)},
    "tool_reasons": TOOL_REASONS_SCHEMA,
    # Content dependencies only; the program derives the execution order.
    # null means "not stated" and gets the program's safe default.
    "visual_uses_research": OPTIONAL_BOOL,
    "quiz_uses_research": OPTIONAL_BOOL,
    "quiz_uses_visual": OPTIONAL_BOOL,
})


# After explaining a block, the coordinator may ask for one graded check question
# on it; the quiz writer writes it from the reply text and the program grades it.
CHECK_SCHEMA = {"anyOf": [{"type": "null"}, object_schema({"topic": TEXT, "difficulty": DIFFICULTY})]}


def decision_schema(response_schema):
    """Choose one complete public answer or one bounded internal tool plan.

    Either way the coordinator also returns its updated conversation notes
    ("context_notes"; null = no change) and an optional check question.
    """
    return {"anyOf": [
        object_schema({
            "mode": {"type": "string", "const": DecisionMode.ANSWER.value},
            "response": response_schema,
            "task": TASK_SCHEMA,
            "context_notes": CONTEXT_UPDATE_SCHEMA,
            "check_question": CHECK_SCHEMA,
        }),
        object_schema({
            "mode": {"type": "string", "const": DecisionMode.TOOLS.value},
            "plan": PLAN_SCHEMA,
            "initial_explanation": OPTIONAL_TEXT,
            "context_notes": CONTEXT_UPDATE_SCHEMA,
            "check_question": CHECK_SCHEMA,
        }),
    ]}


QUIZ_SCHEMA = object_schema({
    "concept_id": TEXT,
    "question": TEXT,
    "choices": {"type": "array", "items": TEXT, "minItems": 2, "maxItems": 4, "uniqueItems": True},
    "answer_index": {"type": "integer", "minimum": 0, "maximum": 3},
    "explanation": TEXT,
    # One entry per wrong choice: which misconception choosing it suggests.
    "diagnoses": {"type": "array", "minItems": 1, "maxItems": 3, "items": object_schema({
        "choice_index": {"type": "integer", "minimum": 0, "maximum": 3},
        "error_type": {"enum": list(ERROR_TYPES)},
        "misconception": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"},
    })},
    # An AI picture that the question is asked about, or null.
    "image": {"anyOf": [{"type": "null"}, object_schema({
        "prompt": {"type": "string", "minLength": 1, "maxLength": 800, "pattern": r"\S"},
        "alt": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"},
    })]},
})


def check_question(question):
    """Program checks the model cannot be trusted with; returns a fixed problem code or None."""
    count = len(question["choices"])
    if question["answer_index"] >= count:
        return "answer_index_out_of_range"
    marked = [item["choice_index"] for item in question["diagnoses"]]
    wrong = [index for index in range(count) if index != question["answer_index"]]
    if sorted(marked) != wrong:
        return "every_wrong_choice_needs_one_diagnosis"
    return None


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


def schema_problem(text, schema) -> str:
    """Where an output broke its schema: JSON path and rule name only, never values."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return "invalid_json"
    from jsonschema.exceptions import best_match
    error = best_match(Draft202012Validator(schema).iter_errors(value))
    if error is None:
        return ""
    return ("/" + "/".join(str(part) for part in error.absolute_path) + " " + str(error.validator))[:120]


def parse(text, schema):
    if not isinstance(text, str) or len(text) > MAX_RESPONSE_CHARS:
        raise ContractError("invalid_response_size")
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_float)
    except (ValueError, RecursionError):
        raise ContractError("invalid_json") from None
    return validate(value, schema)


def public_quiz(quiz):
    """Do not send the answer key to the agent composing an unanswered question."""
    return {key: quiz[key] for key in ("concept_id", "question", "choices")}


def selected_choice(text, quiz):
    """Index of an unambiguous choice (a letter or the exact choice text), else None."""
    value = text.strip()
    letters = "ABCD"[:len(quiz["choices"])]
    if value.upper() in letters and len(value) == 1:
        return letters.index(value.upper())
    if value in quiz["choices"]:
        return quiz["choices"].index(value)
    return None


def grade_choice(text, quiz):
    """Only unambiguous choices get programmatic grading, never guessed prose."""
    selected = selected_choice(text, quiz)
    return None if selected is None else selected == quiz["answer_index"]
