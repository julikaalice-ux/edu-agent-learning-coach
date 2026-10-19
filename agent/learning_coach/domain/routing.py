"""Program-owned eligibility and completion rules for optional capabilities."""

from copy import deepcopy
from enum import StrEnum
import re

from learning_coach.domain.contracts import (ContractError, DEPENDENCY_FIELDS, PLAN_SCHEMA, DecisionMode, Participant,
                                             QuizAction, VisualSource, EASY, grade_choice, validate)
from learning_coach.domain.research_contracts import Evidence

# Intent taxonomy from prompts/instructions.md: the coordinator classifies every
# turn into exactly one primary intent. Letters never appear bare in logic.
INTENT_CONCEPT = "A"        # 概念求助
INTENT_PROBLEM = "B"        # 解题/实践求助
INTENT_PRACTICE = "C"       # 主动练习/测验
INTENT_PLANNING = "D"       # 学习/复习规划
INTENT_CHAT = "E"           # 闲聊/情绪
INTENT_OUT_OF_SCOPE = "F"   # 越界请求
INTENT_UNCLEAR = "G"        # 需求无法确定
EXPLAIN_INTENTS = INTENT_CONCEPT + INTENT_PROBLEM          # "AB": 讲解后可以插检查题
QUIZ_INTENTS = INTENT_CONCEPT + INTENT_PROBLEM + INTENT_PRACTICE  # "ABC": 允许出题
NON_TEACHING_INTENTS = INTENT_CHAT + INTENT_OUT_OF_SCOPE + INTENT_UNCLEAR  # "EFG": 不启用工具

TOOLS = ("research", "quiz", "notes", "visual")
QUIZ_SET_SIZE = 3  # questions in a practice set


class Status(StrEnum):
    """Where a tool stands in a turn. Every requested tool must end in a terminal status."""
    PENDING = "pending"
    NOT_REQUESTED = "not_requested"
    SKIPPED = "skipped_policy"
    # quiz
    QUESTION_READY = "question_ready"
    AWAITING_ANSWER = "awaiting_answer"
    FEEDBACK_READY = "feedback_ready"
    FINISHED = "finished"
    NEEDS_INPUT = "needs_input"
    # notes
    DEFERRED = "deferred"
    PREPARED = "prepared"
    SAVED = "saved"
    NOT_WRITTEN = "not_written"
    FAILED = "failed"
    # visual (research ends in an Evidence level)
    READY = "ready"
    PUBLISHED = "published"
    UNAVAILABLE = "unavailable"


_NOT_RUN = {Status.NOT_REQUESTED, Status.SKIPPED}
TERMINAL_STATUS = {
    "research": _NOT_RUN | set(Evidence),
    "quiz": _NOT_RUN | {Status.QUESTION_READY, Status.AWAITING_ANSWER, Status.FEEDBACK_READY, Status.FINISHED,
                        Status.NEEDS_INPUT},
    # Notes are compiled after a quiz set with the learner's consent; a turn
    # with learning evidence is only marked ("deferred") for that later run.
    "notes": _NOT_RUN | {Status.DEFERRED, Status.PREPARED, Status.SAVED, Status.NOT_WRITTEN, Status.FAILED},
    "visual": _NOT_RUN | {Status.READY, Status.PUBLISHED, Status.UNAVAILABLE},
}


def empty_results():
    return dict.fromkeys(TOOLS, Status.NOT_REQUESTED)


def conservative_participant(stored, observed):
    """Do not turn known assistance or uncertain authorship into independence."""
    if Participant.COMPANION in (stored, observed):
        return Participant.COMPANION
    if stored == observed == Participant.LEARNER:
        return Participant.LEARNER
    return Participant.UNKNOWN


def _valid_quote(quote, learner_text):
    if quote not in learner_text:
        return False
    size = sum(not char.isspace() for char in quote)
    # A complete one-character reply (A, 好) is valid evidence. A single
    # character taken out of a longer message is too weak even for provenance.
    return size >= 2 or (size == 1 and quote.strip() == learner_text.strip())


def select_tools(plan, learner_text, *, memory_enabled):
    """Check stated need, then apply non-model policy before doing any work.

    An exact quotation establishes provenance, not semantic correctness of the
    model's need assessment. That still requires model-behavior evaluation.
    """
    requested = {
        "research": plan["research_query"] is not None,
        "quiz": plan["quiz_action"] in (QuizAction.START, QuizAction.CONTINUE),
        "notes": plan["observe_learning"],
        "visual": plan["visual_request"] is not None,
    }
    if plan["end_task"] or plan["pause_task"] or plan["quiz_action"] == QuizAction.STOP:
        # Honoring an explicit control action needs no external capability.
        # Ignored tool requests must not prevent pausing/stopping the task.
        effective = deepcopy(plan)
        effective.update(research_query=None, search_queries=None, visual_request=None, observe_learning=False,
                         quiz_action=QuizAction.STOP if plan["quiz_action"] == QuizAction.STOP else QuizAction.NONE)
        return effective, {name: Status.SKIPPED if requested[name] else Status.NOT_REQUESTED for name in TOOLS}
    for name, active in requested.items():
        reason = plan["tool_reasons"][name]
        if active != (reason is not None):
            raise ContractError("tool_reason_mismatch")
        if reason is not None and not _valid_quote(reason["evidence_quote"], learner_text):
            raise ContractError("tool_evidence_invalid")
    if not any(requested.values()):
        raise ContractError("tool_route_without_request")
    if requested["quiz"]:
        continuing = plan["quiz_action"] == QuizAction.CONTINUE
        if (plan["tool_reasons"]["quiz"]["code"] == "continue_practice") != continuing:
            raise ContractError("quiz_reason_action_mismatch")

    allowed = dict(requested)
    if plan["intent"]["primary"] in NON_TEACHING_INTENTS or plan["end_task"] or plan["pause_task"] or plan["quiz_action"] == QuizAction.STOP:
        allowed = dict.fromkeys(TOOLS, False)
    if plan["intent"]["primary"] not in QUIZ_INTENTS:
        allowed["quiz"] = False
    if not memory_enabled:
        allowed["notes"] = False

    effective = deepcopy(plan)
    if not allowed["research"]:
        effective["research_query"] = None
    if effective["research_query"] is None:
        effective["search_queries"] = None
    if not allowed["visual"]:
        effective["visual_request"] = None
    if not allowed["notes"]:
        effective["observe_learning"] = False
    if not allowed["quiz"] and effective["quiz_action"] != QuizAction.STOP:
        effective["quiz_action"] = QuizAction.NONE
    results = {name: Status.PENDING if allowed[name] else Status.SKIPPED if requested[name] else Status.NOT_REQUESTED
               for name in TOOLS}
    return effective, results


def recover_quiz_route(decision, state, learner_text, *, memory_enabled):
    """Repair only a structurally valid routing decision for a known answer.

    No extra model request and no work is performed here. Quiz metadata and
    participant provenance come from program state, never from a direct reply.
    The corrected plan uses the usual transactional grading/commit pipeline.
    """
    quiz = state.get("quiz")
    if not quiz or quiz.get("finished") or not quiz.get("awaiting"):
        return decision, False
    if grade_choice(learner_text, quiz["awaiting"]) is None:
        return decision, False
    controls = decision["task"] if decision["mode"] == DecisionMode.ANSWER else decision["plan"]
    if controls["end_task"] or controls["pause_task"] or controls.get("quiz_action") == QuizAction.STOP:
        return decision, False
    if decision["mode"] == DecisionMode.TOOLS:
        try:
            effective, results = select_tools(decision["plan"], learner_text, memory_enabled=memory_enabled)
        except ContractError:
            pass
        else:
            if effective["quiz_action"] == QuizAction.CONTINUE and results["quiz"] == Status.PENDING:
                return decision, False

    quote = learner_text.strip()
    reasons = dict.fromkeys(TOOLS)
    reasons["quiz"] = {"code": "continue_practice", "evidence_quote": quote}
    if memory_enabled:
        reasons["notes"] = {"code": "learning_evidence", "evidence_quote": quote}
    participant = quiz.get("participant", Participant.UNKNOWN)
    if decision["mode"] == DecisionMode.TOOLS:
        participant = conservative_participant(participant, decision["plan"]["participant"])
    repaired = {
        "intent": {"primary": INTENT_PRACTICE, "is_implicit": True},
        "research_query": None, "search_queries": None, "visual_request": None, "visual_source": None, "observe_learning": memory_enabled,
        "task_summary": quiz["topic"], "pause_task": False, "end_task": False,
        "quiz_action": QuizAction.CONTINUE, "quiz_topic": quiz["topic"], "quiz_total": quiz["total"],
        "difficulty": quiz["difficulty"], "participant": participant,
        "tool_reasons": reasons,
        **dict.fromkeys(DEPENDENCY_FIELDS),
    }
    return {"mode": DecisionMode.TOOLS, "plan": validate(repaired, PLAN_SCHEMA)}, True


def recover_practice_route(decision, state, learner_text):
    """An explicit practice intent must enter the program-owned quiz state machine.

    A direct answer cannot be graded and records no mastery evidence, so a
    structurally valid ``mode=answer`` with primary intent C (and no pause/end)
    is rebuilt as the minimal quiz plan. No extra model request and no side
    effect happens here; quiz metadata comes from program state and the
    learner's own message, never from the reply text. A single-character
    message without a valid quotation is left alone.
    """
    if decision["mode"] != DecisionMode.ANSWER:
        return decision, False
    task = decision["task"]
    if decision["response"]["intent"]["primary"] != INTENT_PRACTICE or task["end_task"] or task["pause_task"]:
        return decision, False
    quiz = state.get("quiz")
    continuing = bool(quiz and not quiz.get("finished"))
    quote = learner_text.strip()
    if not _valid_quote(quote, learner_text):
        return decision, False
    reasons = dict.fromkeys(TOOLS)
    reasons["quiz"] = {"code": "continue_practice" if continuing else "explicit_practice",
                       "evidence_quote": quote}
    plan = {
        "intent": {"primary": INTENT_PRACTICE, "is_implicit": False},
        "research_query": None, "search_queries": None, "visual_request": None, "visual_source": None, "observe_learning": False,
        "task_summary": task["task_summary"], "pause_task": False, "end_task": False,
        "quiz_action": QuizAction.CONTINUE if continuing else QuizAction.START,
        "quiz_topic": quiz["topic"] if continuing else (task.get("task_summary") or "").strip() or None,
        "quiz_total": quiz.get("total", QUIZ_SET_SIZE) if continuing else QUIZ_SET_SIZE,
        "difficulty": quiz.get("difficulty", EASY) if continuing else EASY,
        "participant": quiz.get("participant", Participant.UNKNOWN) if continuing else Participant.LEARNER,
        "tool_reasons": reasons,
        **dict.fromkeys(DEPENDENCY_FIELDS),
    }
    return {"mode": DecisionMode.TOOLS, "plan": validate(plan, PLAN_SCHEMA)}, True


# Program-owned triggers: the coordinator's instructions demand a visual route
# for these, but small models sometimes answer directly anyway (e.g. an ASCII
# sketch). Recovery only fires on an explicit keyword in the learner's own
# message, so implicit or declined requests are never forced.
VISUAL_TRIGGERS = ("画图", "图片", "插图", "配图", "画面", "照片", "示意图", "结构图",
                   "互动图", "可视化", "流程图", "思维导图", "用图解释", "用图说明",
                   "用图展示", "动画", "动图", "互动演示", "记忆卡", "闪卡")
VISUAL_OPT_OUTS = ("不用图", "不要图", "别画", "不用画", "文字说明", "文字版")
_NON_VISUAL_DEMO = re.compile(r"模拟(?:考试|考|试卷|试题|面试)|演示文稿|幻灯片")
_VISUAL_WORD = r"(?:演示|模拟|动画|动图|图片|插图|配图|照片|示意图|结构图|互动图|可视化|流程图|思维导图|画图|用图解释|用图说明|用图展示)"
_VISUAL_DEFINITION = re.compile(
    _VISUAL_WORD + r"[”\"’'」』]?(?:这个词|一词)?(?:是什么(?:意思|含义)?|什么意思|的意思|的含义|的定义|怎么定义)"
    r"|(?:什么是|什么叫)[“\"‘'「『]?" + _VISUAL_WORD + r"[”\"’'」』]?(?:呢|啊|呀)?$"
)
_VISUAL_DECLINE = re.compile(
    r"(?:不要|不需要|不想看|不想要|不用|不必|别|停止|取消).{0,8}" + _VISUAL_WORD
    + r"|" + _VISUAL_WORD + r".{0,6}(?:不要了|不用了|不需要了|先不看|取消)"
)
_DEMO_REQUEST = re.compile(
    r"(?:演示|模拟)(?:一下|一遍|给我看)"
    r"|(?:^|请|帮我|给我|能否|可以|能不能)(?:做(?:个|一个)?|进行(?:一次)?)?(?:演示|模拟)"
    r"|(?:演示|模拟).{0,100}(?:原理|过程|变化|形成|运动|发生|传播|作用|如何|怎么)"
    r"|(?:原理|过程|变化|形成|运动|传播).{0,30}(?:演示|模拟)"
)


def visual_request_clauses(learner_text):
    """Yield request clauses, excluding declined visuals and non-visual uses."""
    # These are conservative recovery rules, not a subject-to-template router.
    # Inspect clauses separately so "不要照片，用动画解释" still requests a visual.
    for clause in re.split(r"[，,。；;！？!?\n]+|但是|但|不过|而是", learner_text.strip()):
        if (any(opt in clause for opt in VISUAL_OPT_OUTS)
                or _NON_VISUAL_DEMO.search(clause) or _VISUAL_DEFINITION.search(clause)
                or _VISUAL_DECLINE.search(clause)):
            continue
        if clause.strip():
            yield clause


def _visual_quote(learner_text):
    """Return a verbatim trigger quote from the learner's message, or None."""
    for clause in visual_request_clauses(learner_text):
        for trigger in VISUAL_TRIGGERS:
            if trigger in clause:
                return trigger
        match = _DEMO_REQUEST.search(clause)
        if match:
            return match.group(0)
    return None


# Asking to see what a real thing looks like: an existing picture beats an AI one.
_REAL_LOOK = re.compile(r"照片|相片|实拍|显微|长什么样|长啥样|什么样子|实物|标本|文物|真实的样子|外观")


def prefer_web_pictures(plan, learner_text):
    """An AI picture planned for a request to see a real thing becomes a web picture.

    The coordinator is told the same rule but did not always follow it; an AI
    picture of a cell or an animal is also the kind the reviewer rejects.
    """
    if (plan.get("visual_request") and plan.get("visual_source") == VisualSource.GENERATED
            and _REAL_LOOK.search(learner_text or "")):
        return {**plan, "visual_source": VisualSource.WEB}
    return plan


def explicit_visual_quote(learner_text):
    """The learner's own words that ask for a picture or a demonstration, or None."""
    return _visual_quote(learner_text)


def recover_explicit_visual_route(decision, learner_text):
    """An explicit diagram/chart request must not be answered with plain text.

    A structurally valid ``mode=answer`` whose message carries an explicit
    visual keyword is rebuilt as the minimal visual plan. No extra model
    request and no side effect happens here; the evidence quote is the
    learner's own word, matching the ``explicit_visual`` reason contract.
    """
    if decision["mode"] != DecisionMode.ANSWER:
        return decision, False
    task = decision["task"]
    if task["end_task"] or task["pause_task"]:
        return decision, False
    if decision["response"]["intent"]["primary"] in NON_TEACHING_INTENTS:
        return decision, False
    quote = _visual_quote(learner_text)
    if quote is None:
        return decision, False
    reasons = dict.fromkeys(TOOLS)
    reasons["visual"] = {"code": "explicit_visual", "evidence_quote": quote}
    plan = {
        "intent": {"primary": INTENT_CONCEPT, "is_implicit": False},
        "research_query": None, "search_queries": None, "visual_request": learner_text.strip(), "visual_source": VisualSource.LOCAL,
        "observe_learning": False,
        "task_summary": task["task_summary"], "pause_task": False, "end_task": False,
        "quiz_action": QuizAction.NONE, "quiz_topic": None, "quiz_total": QUIZ_SET_SIZE,
        "difficulty": EASY, "participant": Participant.LEARNER,
        "tool_reasons": reasons,
        **dict.fromkeys(DEPENDENCY_FIELDS),
    }
    return {"mode": DecisionMode.TOOLS, "plan": validate(plan, PLAN_SCHEMA)}, True


def require_finished(results):
    """Do not publish a final response while a selected capability is pending."""
    if set(results) != set(TOOLS) or any(status not in TERMINAL_STATUS[name] for name, status in results.items()):
        raise ContractError("tool_result_not_terminal")


def execution_order(plan):
    """Derive waits from the coordinator's content dependencies.

    The coordinator only says what depends on what; the program decides the
    order. Notes never wait. A visual or question waits for this turn's
    research unless the coordinator explicitly says it does not use it
    (research only runs for uncertain or time-sensitive facts, so an unstated
    dependency is treated as real). A question waits for the visual only when
    the coordinator explicitly says it is answered from the picture. A wait
    on a capability that does not run this turn is dropped, and waits only
    point forward (research -> visual -> quiz), so they cannot deadlock.
    """
    research = plan["research_query"] is not None
    visual = plan["visual_request"] is not None
    quiz = plan["quiz_action"] in (QuizAction.START, QuizAction.CONTINUE)
    return {
        "visual_waits_research": research and visual and plan.get("visual_uses_research") is not False,
        "quiz_waits_research": research and quiz and plan.get("quiz_uses_research") is not False,
        "quiz_waits_visual": visual and quiz and plan.get("quiz_uses_visual") is True,
    }
