"""First-meeting questions: who is learning, their stage, and record consent.

A small program-owned state machine. The model never decides whether a
learner consented: only an answer this module recognizes turns records on.
Unrecognized answers repeat the same question; "跳过" ends the questions with
records off. The 14-year line follows the guardian-consent threshold for
minors' personal information; school stages only approximate age, so junior
high and "不想说" are treated as possibly under 14.
"""

import re

SELF_STAGES = ("学龄前", "小学", "初中", "高中", "大学", "已工作", "不想说")
CHILD_STAGES = ("学龄前", "小学", "初中", "高中")
MAYBE_UNDER_14 = {"学龄前", "小学", "初中", "不想说"}
GREETINGS = re.compile(r"^(?:你好|您好|嗨|哈喽|hi|hello|hey|在吗|在不在|早上好|晚上好|下午好)[!！。.~～？?\s]*$",
                       re.IGNORECASE)
SKIP = re.compile(r"^\s*(?:跳过|先跳过|不想回答|不回答|skip)\s*$", re.IGNORECASE)

QUESTIONS = {
    "who": ("这次是谁在学？", ("我自己", "我是家长或老师，帮孩子用")),
    "stage_self": ("你现在在哪个阶段？", SELF_STAGES),
    "stage_child": ("孩子现在在哪个阶段？", CHILD_STAGES),
    "guardian": ("保存学习记录需要家长同意。家长现在在身边吗？", ("在，请家长来确认", "不在")),
    "consent_self": ("要保存你的学习进度吗？只记学习情况，不记个人信息，随时可以关掉或删除。",
                     ("保存", "先不保存")),
    "consent_guardian": ("请家长确认：要保存孩子的学习进度吗？只记学习情况，随时可以关掉或删除。",
                         ("保存", "先不保存")),
}


def question_text(step: str) -> str:
    title, options = QUESTIONS[step]
    lines = [title] + [f"{index}. {option}" for index, option in enumerate(options, 1)]
    return "\n".join(lines) + "\n（回复数字或选项文字；也可以回复“跳过”）"


def _choose(step: str, answer: str):
    """Return the chosen option text, or None when the answer is not clear."""
    options = QUESTIONS[step][1]
    value = answer.strip().rstrip("。.!！")
    if value.isdigit() and 1 <= int(value) <= len(options):
        return options[int(value) - 1]
    if value in options:
        return value
    matches = [option for option in options if value and (value == option or value in option.split("，")[0])]
    if len(matches) == 1:
        return matches[0]
    if step.startswith("consent") and value in ("好", "好的", "可以", "要", "保存吧", "同意", "行"):
        return "保存"
    if step.startswith("consent") and value in ("不要", "不用", "不了", "不保存", "算了"):
        return "先不保存"
    if step == "guardian" and value in ("在", "家长在", "我是家长"):
        return options[0]
    return None


def start(first_message: str) -> dict:
    """Begin the questions; keep a real first question to answer afterwards."""
    text = first_message.strip()
    pending = None if (not text or GREETINGS.match(text) or SKIP.match(text)) else text[:4000]
    return {"step": "who", "pending_question": pending}


def advance(record: dict, answer: str) -> dict:
    """Apply one answer. Returns a new record; record["step"] == "done" when finished."""
    record = dict(record)
    if SKIP.match(answer):
        record.update(step="done", records="off", stage=record.get("stage"), skipped=True)
        return record
    step = record["step"]
    lookup = {"who": "who", "stage_self": "stage_self", "stage_child": "stage_child",
              "guardian": "guardian", "consent_self": "consent_self",
              "consent_guardian": "consent_guardian"}[step]
    choice = _choose(lookup, answer)
    if choice is None:
        record["retry"] = True
        return record
    record.pop("retry", None)
    if step == "who":
        record["who"] = "self" if choice == "我自己" else "companion"
        record["step"] = "stage_self" if record["who"] == "self" else "stage_child"
    elif step == "stage_child":
        record.update(stage=choice, stage_answer=answer.strip(), step="consent_guardian")
    elif step == "stage_self":
        record.update(stage=choice, stage_answer=answer.strip())
        record["step"] = "guardian" if choice in MAYBE_UNDER_14 else "consent_self"
    elif step == "guardian":
        if choice == "不在":
            record.update(step="done", records="off", guardian_absent=True)
        else:
            record["step"] = "consent_guardian"
    else:
        record.update(step="done", records="on" if choice == "保存" else "off",
                      consent_by="guardian" if step == "consent_guardian" else "self")
    return record


def closing_message(record: dict) -> str:
    if record.get("records") == "on":
        return "好，学习进度会保存下来，下次可以接着学。想关掉随时说“关闭记录”。"
    if record.get("guardian_absent"):
        return "好，这次先不保存学习记录，照常学习。以后家长在身边时，可以说“开启记录”。"
    return "好，这次不保存学习记录。想保存时随时说“开启记录”。"
