"""Program checks on a reply before anyone sees it."""

import re

from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.routing import INTENT_UNCLEAR
from learning_coach.service.pipeline.state import Key
from learning_coach.service.pipeline.texts import STEP_QUESTION

_LEADING_REFERENCE = re.compile(r"^[这那它](?:些|们)?")
_CLAIMS_SAVED = re.compile(r"已(?:记下|记录|保存)|(?:先|马上|替你|帮你)(?:记下|记录|保存)|(?:记下|保存)(?:了|啦|哦)|记住(?:了|啦)|记好(?:了|啦)")
_URL = re.compile(r"https?://[^\s)<>]+", re.IGNORECASE)


def correct_choice(quiz_question) -> str | None:
    """The text of the correct choice, or None when the question has none."""
    if not isinstance(quiz_question, dict):
        return None
    choices = quiz_question.get("choices")
    index = quiz_question.get("answer_index")
    if not isinstance(choices, list) or not isinstance(index, int) or not 0 <= index < len(choices):
        return None
    return str(choices[index]).strip()


def choice_key(choice: str) -> str:
    """A choice minus leading pronouns and whitespace, for leak matching.

    "它们都属于猫科" and "猫和老虎都属于猫科" name the same answer, so an
    exact substring check misses the leak; stripping the reference word and
    whitespace from both sides catches it.
    """
    return re.sub(r"\s+", "", _LEADING_REFERENCE.sub("", choice.strip()))


def gives_away(choice: str | None, text: str) -> bool:
    """True when text contains the correct choice (after normalizing both)."""
    return bool(choice and choice_key(choice) and choice_key(choice) in re.sub(r"\s+", "", text))


def respond_problem(output, state, memory_enabled) -> str | None:
    """Deterministic rejections for a reply draft, retried once with this correction.

    Two rules only: never show the correct choice of the question that is
    still waiting for an answer, and never claim records were saved when
    they are off or not yet committed.
    """
    message = output.get("message", "") if isinstance(output, dict) else ""
    if gives_away(correct_choice((state.get(Key.QUIZ) or {}).get("awaiting")), message):
        return "回复正文不得出现当前等待作答题目的正确答案；说明和反馈请避开正确选项原句。"
    if not memory_enabled and _CLAIMS_SAVED.search(message):
        return "学习记录未开启或尚未提交，回复不得声称已记下或已保存。"
    return None


def validate_response(output, schema, intent, research, *, stopping=False):
    """Schema, the routed intent, a question when asking for clarification, and only retrieved URLs."""
    validate(output, schema)
    if output["intent"] != intent:
        raise ContractError("intent_changed_after_routing")
    if (not stopping and intent["primary"] == INTENT_UNCLEAR
            and (not output["next_step"] or output["next_step"]["kind"] != STEP_QUESTION)):
        raise ContractError("clarification_requires_question")
    allowed = {source["url"] for source in (research or {}).get("sources", [])}
    for url in _URL.findall(output["message"] + (output["next_step"] or {}).get("text", "")):
        if url not in allowed:
            raise ContractError("unretrieved_url")
