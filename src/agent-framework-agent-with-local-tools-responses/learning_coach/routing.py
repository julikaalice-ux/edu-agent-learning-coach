"""Program-owned eligibility and completion rules for optional capabilities."""

from copy import deepcopy

from .contracts import ContractError, PLAN_SCHEMA, grade_choice, validate

TOOLS = ("research", "quiz", "notes", "visual")
TERMINAL_STATUS = {
    "research": {"not_requested", "skipped_policy", "verified", "likely", "disputed", "unavailable"},
    "quiz": {"not_requested", "skipped_policy", "question_ready", "awaiting_answer", "feedback_ready", "finished", "needs_input"},
    "notes": {"not_requested", "skipped_policy", "prepared", "saved", "not_written", "failed"},
    "visual": {"not_requested", "skipped_policy", "ready", "published", "unavailable"},
}


def empty_results():
    return dict.fromkeys(TOOLS, "not_requested")


def conservative_participant(stored, observed):
    """Do not turn known assistance or uncertain authorship into independence."""
    if "companion_report" in (stored, observed):
        return "companion_report"
    if stored == observed == "learner":
        return "learner"
    return "unknown"


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
        "quiz": plan["quiz_action"] in ("start", "continue"),
        "notes": plan["observe_learning"],
        "visual": plan["visual_request"] is not None,
    }
    if plan["end_task"] or plan["pause_task"] or plan["quiz_action"] == "stop":
        # Honoring an explicit control action needs no external capability.
        # Ignored tool requests must not prevent pausing/stopping the task.
        effective = deepcopy(plan)
        effective.update(research_query=None, visual_request=None, observe_learning=False,
                         quiz_action="stop" if plan["quiz_action"] == "stop" else "none")
        return effective, {name: "skipped_policy" if requested[name] else "not_requested" for name in TOOLS}
    for name, active in requested.items():
        reason = plan["tool_reasons"][name]
        if active != (reason is not None):
            raise ContractError("tool_reason_mismatch")
        if reason is not None and not _valid_quote(reason["evidence_quote"], learner_text):
            raise ContractError("tool_evidence_invalid")
    if not any(requested.values()):
        raise ContractError("tool_route_without_request")
    if requested["quiz"]:
        continuing = plan["quiz_action"] == "continue"
        if (plan["tool_reasons"]["quiz"]["code"] == "continue_practice") != continuing:
            raise ContractError("quiz_reason_action_mismatch")

    allowed = dict(requested)
    if plan["intent"]["primary"] in "EFG" or plan["end_task"] or plan["pause_task"] or plan["quiz_action"] == "stop":
        allowed = dict.fromkeys(TOOLS, False)
    if plan["intent"]["primary"] not in "ABC":
        allowed["quiz"] = False
    if not memory_enabled:
        allowed["notes"] = False

    effective = deepcopy(plan)
    if not allowed["research"]:
        effective["research_query"] = None
    if not allowed["visual"]:
        effective["visual_request"] = None
    if not allowed["notes"]:
        effective["observe_learning"] = False
    if not allowed["quiz"] and effective["quiz_action"] != "stop":
        effective["quiz_action"] = "none"
    results = {name: "pending" if allowed[name] else "skipped_policy" if requested[name] else "not_requested"
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
    controls = decision["task"] if decision["mode"] == "answer" else decision["plan"]
    if controls["end_task"] or controls["pause_task"] or controls.get("quiz_action") == "stop":
        return decision, False
    if decision["mode"] == "tools":
        try:
            effective, results = select_tools(decision["plan"], learner_text, memory_enabled=memory_enabled)
        except ContractError:
            pass
        else:
            if effective["quiz_action"] == "continue" and results["quiz"] == "pending":
                return decision, False

    quote = learner_text.strip()
    reasons = dict.fromkeys(TOOLS)
    reasons["quiz"] = {"code": "continue_practice", "evidence_quote": quote}
    if memory_enabled:
        reasons["notes"] = {"code": "learning_evidence", "evidence_quote": quote}
    participant = quiz.get("participant", "unknown")
    if decision["mode"] == "tools":
        participant = conservative_participant(participant, decision["plan"]["participant"])
    repaired = {
        "intent": {"primary": "C", "is_implicit": True},
        "research_query": None, "visual_request": None, "observe_learning": memory_enabled,
        "task_summary": quiz["topic"], "pause_task": False, "end_task": False,
        "quiz_action": "continue", "quiz_topic": quiz["topic"], "quiz_total": quiz["total"],
        "difficulty": quiz["difficulty"], "participant": participant,
        "tool_reasons": reasons,
    }
    return {"mode": "tools", "plan": validate(repaired, PLAN_SCHEMA)}, True


def recover_practice_route(decision, state, learner_text):
    """An explicit practice intent must enter the program-owned quiz state machine.

    A direct answer cannot be graded and records no mastery evidence, so a
    structurally valid ``mode=answer`` with primary intent C (and no pause/end)
    is rebuilt as the minimal quiz plan. No extra model request and no side
    effect happens here; quiz metadata comes from program state and the
    learner's own message, never from the reply text. A single-character
    message without a valid quotation is left alone.
    """
    if decision["mode"] != "answer":
        return decision, False
    task = decision["task"]
    if decision["response"]["intent"]["primary"] != "C" or task["end_task"] or task["pause_task"]:
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
        "intent": {"primary": "C", "is_implicit": False},
        "research_query": None, "visual_request": None, "observe_learning": False,
        "task_summary": task["task_summary"], "pause_task": False, "end_task": False,
        "quiz_action": "continue" if continuing else "start",
        "quiz_topic": quiz["topic"] if continuing else task["task_summary"].strip() or None,
        "quiz_total": quiz.get("total", 3) if continuing else 3,
        "difficulty": quiz.get("difficulty", "简单") if continuing else "简单",
        "participant": quiz.get("participant", "unknown") if continuing else "learner",
        "tool_reasons": reasons,
    }
    return {"mode": "tools", "plan": validate(plan, PLAN_SCHEMA)}, True


def require_finished(results):
    """Do not publish a final response while a selected capability is pending."""
    if set(results) != set(TOOLS) or any(status not in TERMINAL_STATUS[name] for name, status in results.items()):
        raise ContractError("tool_result_not_terminal")
