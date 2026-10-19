"""Session state: what each key holds, and the small reads and writes on it.

The state dict is what clients send back every turn, so its keys stay as they
are. Grouped by what they are for:

  phase      quiz, check_offer, notes_offer      where the lesson is; an offer lasts one turn
  interrupt  pending_external, pending_forget,   a question the program asked, answered
             onboarding (until step == "done")   before anything else
  task       active_task, pending_tasks, resume_stage
  settings   memory_blocked, onboarding (once done)
  context    context                              conversation notes, goal, quiz results,
                                                  error history, evidence turns
  replay     last_event, last_input, last_response, revision, owner_session (shell only)
  report     last_execution                       codes for logs and evaluations
"""

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum

from learning_coach.domain.contracts import Participant, QuizAction, public_quiz
from learning_coach.domain.note_contracts import SCHEMA_VERSION as NOTES_VERSION
from learning_coach.domain.onboarding import WHO_COMPANION
from learning_coach.domain.routing import INTENT_CHAT


class Key:
    # phase
    QUIZ = "quiz"
    CHECK_OFFER = "check_offer"
    NOTES_OFFER = "notes_offer"
    # interrupt
    PENDING_EXTERNAL = "pending_external"
    PENDING_FORGET = "pending_forget"
    ONBOARDING = "onboarding"
    # task
    ACTIVE_TASK = "active_task"
    PENDING_TASKS = "pending_tasks"
    RESUME_STAGE = "resume_stage"
    # settings
    MEMORY_BLOCKED = "memory_blocked"
    # context
    CONTEXT = "context"
    # replay (written by the shell only)
    LAST_EVENT = "last_event"
    LAST_INPUT = "last_input"
    LAST_RESPONSE = "last_response"
    REVISION = "revision"
    OWNER = "owner_session"
    # report
    LAST_EXECUTION = "last_execution"


class Mode(StrEnum):
    """last_execution["mode"]: which kind of turn ran."""
    ANSWER = "answer"                    # the coordinator replied directly
    TOOLS = "tools"                      # tools ran, then the coordinator replied
    CHECK_QUESTION = "check_question"    # the learner took a check offer
    SINGLE = "single"                    # the single-agent baseline


USER, ASSISTANT = "user", "assistant"   # the two message roles kept in the history

# state["resume_stage"]: whether the active task is being worked on or paused.
TASK_ACTIVE, TASK_PAUSED = "active", "paused"

# Kept out of everything a model sees.
PRIVATE_KEYS = (Key.LAST_EVENT, Key.LAST_INPUT, Key.LAST_RESPONSE, Key.LAST_EXECUTION, Key.CONTEXT,
                Key.PENDING_EXTERNAL)

# How much history the program keeps in state.
KEEP_QUESTIONS = 20         # questions remembered so new ones are not repeats
KEEP_ERRORS = 20            # suspected errors, for "the same mistake again"
KEEP_EVIDENCE_TURNS = 20    # turns marked as learning evidence for the notes
KEEP_QUIZ_RESULTS = 5
KEEP_FINISHED_TASKS = 5
KEEP_PAUSED_TASKS = 5
GOAL_CHARS = 300
# Which parts of the conversation notes a sub-agent sees.
NOTES_FOR_QUESTIONS = ("covered", "stuck")      # quiz writer, notes agent
NOTES_FOR_VISUALS = ("covered", "requests")     # visual designer
OFFER_EXPLANATION_CHARS = 3000  # the explanation a check question is written from


@dataclass
class Memory:
    """Whether learning records are on this turn, and what they hold."""
    enabled: bool
    snapshot: dict


def empty_snapshot() -> dict:
    """What the program reads when records are off."""
    return {"schema_version": NOTES_VERSION, "profile": {}, "concepts": [], "error_records": [], "review_plan": []}


def empty_learning_notes() -> dict:
    """An empty write (profile is a list in writes, a dict in reads)."""
    return {"schema_version": NOTES_VERSION, "profile": [], "concepts": [], "error_records": [], "review_plan": []}


def memory_enabled(state, store, memory_key) -> bool:
    return not state.get(Key.MEMORY_BLOCKED, False) and store.is_enabled(memory_key)


def read_memory(state, store, memory_key) -> Memory:
    enabled = memory_enabled(state, store, memory_key)
    return Memory(enabled, store.snapshot(memory_key) if enabled else empty_snapshot())


def is_first_meeting(turn, memory: Memory) -> bool:
    """This learner talks to the coach for the first time: the client has not seen
    them before, there is no reply yet in this conversation and nothing remembered."""
    return (not turn.returning_visitor and Key.ONBOARDING not in turn.state
            and not any(item["role"] == ASSISTANT for item in turn.messages)
            and not (memory.snapshot.get("profile") or memory.snapshot.get("concepts")))


def running_quiz(state) -> dict | None:
    quiz = state.get(Key.QUIZ)
    return quiz if quiz and not quiz.get("finished") else None


def waiting_question(state) -> dict | None:
    """The question the learner still owes an answer to."""
    quiz = running_quiz(state)
    return quiz.get("awaiting") if quiz else None


def default_participant(state) -> str:
    """Who answers a new question when no plan says so: a companion only when one said so."""
    quiz = state.get(Key.QUIZ) or {}
    if quiz.get("participant") in set(Participant) and not quiz.get("finished"):
        return quiz["participant"]
    return (Participant.COMPANION if (state.get(Key.ONBOARDING) or {}).get("who") == WHO_COMPANION
            else Participant.LEARNER)


def model_view(state) -> dict:
    """The state a model may see: no replay data, no context, no answer key."""
    visible = deepcopy({key: value for key, value in state.items() if key not in PRIVATE_KEYS})
    quiz = visible.get(Key.QUIZ)
    if quiz and quiz.get("awaiting"):
        quiz["awaiting"] = public_quiz(quiz["awaiting"])
    return visible


# ---------------------------------------------------------------- task block

def apply_task(state, primary, task):
    """Start, pause, resume or end the current task as the decision says."""
    if task["end_task"] or task.get("quiz_action") == QuizAction.STOP:
        state.pop(Key.QUIZ, None)
        state[Key.ACTIVE_TASK] = None
        state[Key.PENDING_TASKS] = []
        state.pop(Key.RESUME_STAGE, None)
    elif primary == INTENT_CHAT or task["pause_task"]:
        if state.get(Key.ACTIVE_TASK):
            state[Key.RESUME_STAGE] = TASK_PAUSED
        if task["task_summary"]:
            pending = state.setdefault(Key.PENDING_TASKS, [])
            if task["task_summary"] not in pending:
                pending.append(task["task_summary"])
            state[Key.PENDING_TASKS] = pending[-KEEP_PAUSED_TASKS:]
    elif task["task_summary"]:
        state[Key.ACTIVE_TASK] = task["task_summary"]
        state[Key.RESUME_STAGE] = TASK_ACTIVE
        state[Key.PENDING_TASKS] = [item for item in state.get(Key.PENDING_TASKS, [])
                                    if item != task["task_summary"]]


def track_tasks(state, previous_task, previous_quiz, turn, text):
    """Program-kept task cards and the learner's goal in their own words."""
    context = state.setdefault(Key.CONTEXT, {})
    active = state.get(Key.ACTIVE_TASK)
    if active and active != previous_task:
        context["goal"] = {"turn": turn, "text": text[:GOAL_CHARS]}
    if previous_task and not active and not state.get(Key.PENDING_TASKS, []).count(previous_task):
        card = {"task": previous_task, "status": "已结束"}
        if previous_quiz and previous_quiz.get("answered"):
            card["quiz"] = f"{previous_quiz['answered']} 题对 {previous_quiz['correct']} 题"
        context["finished"] = (context.get("finished", []) + [card])[-KEEP_FINISHED_TASKS:]


def mark_evidence(context, turn):
    """This turn may count as learning evidence when notes are compiled later."""
    marks = [item for item in context.get("evidence_turns", []) if item != turn]
    context["evidence_turns"] = (marks + [turn])[-KEEP_EVIDENCE_TURNS:]


def record_quiz_result(context, *, topic, kind, answered, correct, errors):
    results = context.get("quiz_results", [])
    results.append({"topic": topic, "kind": kind, "answered": answered, "correct": correct,
                    "suspected_errors": errors})
    context["quiz_results"] = results[-KEEP_QUIZ_RESULTS:]
