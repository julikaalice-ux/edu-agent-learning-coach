"""Quizzes: creating a set, grading, writing and showing questions, the check offer, “换一题”.

The program owns everything that decides a result: which set is running, the
answer key, grading, and when a set is finished. The quiz writer only drafts
a question, which the program checks before it is shown.
"""

import asyncio
from copy import deepcopy
import logging

from learning_coach.domain.contracts import (QUIZ_SCHEMA, ContractError, Participant, QuizAction, QuizKind,
                                             check_question, public_quiz, selected_choice, validate)
from learning_coach.domain.context import brief_card, note_slice, number_turns, recent_window
from learning_coach.domain.note_contracts import ERROR_LABELS
from learning_coach.domain.routing import (INTENT_CHAT, INTENT_PRACTICE, QUIZ_INTENTS, conservative_participant,
                                           empty_results)
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.service.execution import Capability, StageFailure, external_allowed
from learning_coach.service.pipeline.checks import correct_choice, gives_away
from learning_coach.service.pipeline.state import (KEEP_ERRORS, KEEP_QUESTIONS, NOTES_FOR_QUESTIONS,
                                                   OFFER_EXPLANATION_CHARS, Key, Mode,
                                                   default_participant, empty_learning_notes, read_memory)
from learning_coach.service.pipeline.texts import (CHECK_OFFER, QUIZ_PICTURE_LINK, question, render_question,
                                                   reply)
from learning_coach.service.ports import CallPhase, StorageError

LOG = logging.getLogger(__name__)

PICTURE_TIMEOUT_SECONDS = 60
MAX_REJECTED_DRAFTS = 2     # sound-question tries; one more only to drop an undrawable picture
# Corrections the quiz writer gets back (named in its prompt).
NO_PICTURE = "picture_not_available_write_text_only"
REPEATED = "repeated_question"
ANSWER_IN_EXPLANATION = "answer_in_explanation"
CHECK_QUESTIONS = 1         # a check is a single question
REPEATED_ERROR_AT = 2       # the same suspected error this many times counts as repeated
# How the learning records name who answered.
ACTOR = {Participant.LEARNER: "learner", Participant.COMPANION: "companion", Participant.UNKNOWN: "unknown"}
QUIZ_EVIDENCE = "quiz"      # the records' evidence kind for a graded answer
PICTURE_KIND = "illustration"  # a question picture is an AI picture


def new_quiz(*, kind, topic, total, difficulty, participant, history) -> dict:
    """The one place a quiz set is created."""
    return {"topic": topic, "total": total, "difficulty": difficulty, "asked": 0, "answered": 0,
            "correct": 0, "awaiting": None, "finished": False, "question_history": list(history),
            "participant": participant, "kind": kind, "errors": []}


def recent_questions(quiz) -> list:
    return list((quiz or {}).get("question_history", [])[-KEEP_QUESTIONS:])


class QuizService:
    def __init__(self, deps, *, draft):
        self.deps = deps
        # draft(request) -> question draft; the pipeline decides which agent writes it.
        self.draft = draft

    # ------------------------------------------------------------ grading (no model)

    def grade(self, plan, text, state):
        """Program-owned grading and quiz state; no model call.

        Returns (context, evidence, quiz). quiz is set only when this turn still
        needs a new question, which write_question generates afterwards. A wrong
        answer carries the suspected error type marked on the chosen option.
        """
        if plan["intent"]["primary"] not in QUIZ_INTENTS or plan["end_task"] or plan["pause_task"]:
            return None, None, None
        action = plan["quiz_action"]
        if action not in (QuizAction.START, QuizAction.CONTINUE):
            return None, None, None
        quiz = state.get(Key.QUIZ)
        # A check question left unanswered gives way to a practice set the learner asked for.
        replace_check = (action == QuizAction.START and quiz and quiz.get("kind") == QuizKind.CHECK
                         and not quiz.get("finished"))
        # continue without an active quiz is degraded to start: the learner
        # asked for more practice, so a missing state record must not kill the turn.
        if not quiz or quiz.get("finished") or replace_check:
            if not plan["quiz_topic"]:
                return {"needs_topic": True}, None, None
            quiz = state[Key.QUIZ] = new_quiz(kind=QuizKind.PRACTICE, topic=plan["quiz_topic"], total=plan["quiz_total"],
                                              difficulty=plan["difficulty"], participant=plan["participant"],
                                              history=recent_questions(quiz))
        quiz["participant"] = conservative_participant(quiz.get("participant", Participant.UNKNOWN),
                                                       plan["participant"])
        # Grading evidence, notes, new questions and the final reply must agree
        # on authorship, including on a normal (unrepaired) continuation.
        plan["participant"] = quiz["participant"]
        evidence = None
        if quiz.get("awaiting"):
            waiting = quiz["awaiting"]
            selected = selected_choice(text, waiting)
            if selected is None:
                return {"awaiting": public_quiz(waiting), "needs_clear_choice": True}, None, None
            correct = selected == waiting["answer_index"]
            quiz["answered"] += 1
            quiz["correct"] += int(correct)
            quiz["awaiting"] = None
            evidence = {"concept_id": waiting["concept_id"], "correct": correct,
                        "difficulty": quiz["difficulty"], "independent": plan["participant"] == Participant.LEARNER,
                        "actor": ACTOR[plan["participant"]], "kind": QUIZ_EVIDENCE,
                        "question_snapshot": {**waiting, "image_url": quiz.get("awaiting_picture_url")}, "selected": selected, "topic": quiz["topic"]}
            feedback = {"correct": correct, "explanation": waiting["explanation"]}
            if not correct:
                self._suspect_error(waiting, selected, quiz, state, evidence, feedback)
            if quiz["answered"] >= quiz["total"]:
                quiz["finished"] = True
            # Feedback occupies this turn; never advance before error diagnosis.
            if not correct or quiz["finished"]:
                return {"feedback": feedback, "answered": quiz["answered"], "correct_count": quiz["correct"],
                        "finished": quiz["finished"], "needs_review": not correct,
                        "kind": quiz.get("kind", QuizKind.PRACTICE)}, evidence, None
        if quiz.get("finished"):
            return None, evidence, None
        return None, evidence, quiz

    @staticmethod
    def _suspect_error(waiting, selected, quiz, state, evidence, feedback):
        diagnosis = next((item for item in waiting.get("diagnoses", []) if item["choice_index"] == selected), None)
        if not diagnosis:
            return
        # Suspected, not confirmed: it comes from the option chosen. The
        # learner's own reasoning is what confirms it later.
        evidence["error_category"] = diagnosis["error_type"]
        record = {"concept_id": waiting["concept_id"], "error_type": diagnosis["error_type"],
                  "misconception": diagnosis["misconception"]}
        quiz.setdefault("errors", []).append(record)
        context = state.setdefault(Key.CONTEXT, {})
        history = (context.get("error_history", []) + [record])[-KEEP_ERRORS:]
        context["error_history"] = history
        feedback["suspected_error"] = {"type": ERROR_LABELS[diagnosis["error_type"]],
                                       "code": diagnosis["error_type"],
                                       "misconception": diagnosis["misconception"],
                                       "basis": "所选选项对应的常见误解，尚未经学习者确认"}
        repeats = sum(item["concept_id"] == record["concept_id"] and item["error_type"] == record["error_type"]
                      for item in history)
        if repeats >= REPEATED_ERROR_AT:
            feedback["repeated_error"] = True

    def save_evidence(self, turn, evidence, memory_enabled, output):
        """Write a graded answer to the learning records; says so in the reply when it fails."""
        if evidence is None or not memory_enabled:
            return
        try:
            # Records under a learner ID span conversations, so the turn key
            # then includes the conversation to keep turns distinct.
            turn_key = turn.event_id if turn.memory_key == turn.session_id else f"{turn.session_id}:{turn.event_id}"
            self.deps.store.apply_notes(turn.memory_key, turn_key, empty_learning_notes(), turn.text,
                                   quiz_evidence=evidence)
        except (ValueError, OSError, StorageError):
            output["message"] += "\n\n本轮做题记录未保存。"

    # ------------------------------------------------------------ writing a question

    def request_context(self, turn, memory, participant) -> dict:
        """What the quiz writer sees besides the quiz itself."""
        return {"learner": brief_card(turn.state, memory, participant),
                "context_notes": note_slice((turn.state.get(Key.CONTEXT) or {}).get("notes"), *NOTES_FOR_QUESTIONS),
                "recent_messages": self.recent_messages(turn)}

    def recent_messages(self, turn):
        return recent_window(number_turns(turn.messages), self.deps.budgets.sub_window, min_turns=1,
                             message_max=self.deps.budgets.message_max)

    async def write_question(self, participant, quiz, context=None, *, session_id=None):
        """Ask the quiz writer for one new question.

        Returns (public question context, picture artifact or None). The program
        checks what the model cannot be trusted with: one answer in range, every
        wrong choice diagnosed exactly once, no repeated question, the answer not
        already in the explanation. A requested picture is drawn here; if that is
        not possible the writer is asked once more for a text-only question.
        """
        seen = quiz.setdefault("question_history", [])
        known = {item.strip().casefold() for item in seen}
        request = {"phase": CallPhase.QUIZ, "kind": quiz.get("kind", QuizKind.PRACTICE), "topic": quiz["topic"],
                   "difficulty": quiz["difficulty"], "count": 1, "participant": participant,
                   "previous_questions": list(seen), "require_new_question": True}
        request.update(context or {})
        picture, problem, rejected = None, None, 0
        for _ in range(MAX_REJECTED_DRAFTS + 1):
            if problem:
                request["correction"] = problem
            draft = validate(await self.draft(request), QUIZ_SCHEMA)
            problem = check_question(draft)
            if problem is None and draft["question"].strip().casefold() in known:
                problem = REPEATED
            # A check question whose answer is verbatim in what the coach
            # just said is answered by copying, not by understanding.
            if problem is None and gives_away(correct_choice(draft), request.get("explanation") or ""):
                problem = ANSWER_IN_EXPLANATION
            if problem is None and draft["image"] is not None:
                if request.get("image_unavailable"):
                    problem = NO_PICTURE
                else:
                    picture = await self.picture(session_id, draft)
                    if picture is None:
                        request["image_unavailable"] = True
                        problem = NO_PICTURE
            if problem is None:
                break
            LOG.warning("quiz_draft_rejected reason=%s", problem)
            if problem != NO_PICTURE:
                rejected += 1
                if rejected >= MAX_REJECTED_DRAFTS:
                    raise StageFailure("quiz_question_not_accepted")
        else:
            raise StageFailure("quiz_question_not_accepted")
        quiz["awaiting_picture_url"] = picture["url"] if picture else None
        quiz["awaiting"] = draft
        seen.append(draft["question"])
        quiz["asked"] += 1
        public = public_quiz(draft)
        public["has_picture"] = picture is not None
        return {"new_question": public, "answered": quiz["answered"], "total": quiz["total"]}, picture

    async def picture(self, session_id, quiz_question):
        """Draw the picture a question asks about; None when it cannot be drawn."""
        if self.deps.image_generator is None or session_id is None or not external_allowed(Capability.IMAGE_GENERATION):
            return None
        spec = {"kind": PICTURE_KIND, "title": "题目配图",
                "prompt": quiz_question["image"]["prompt"], "alt": quiz_question["image"]["alt"]}
        try:
            validate(spec, VISUAL_SCHEMA)
            image = await asyncio.wait_for(self.deps.image_generator(spec["prompt"]), timeout=PICTURE_TIMEOUT_SECONDS)
            return self.deps.artifacts.prepare(session_id, spec, image=image)
        except (ContractError, RuntimeError, OSError, TimeoutError, ValueError, StorageError):
            # Provider details never reach logs or the learner.
            LOG.warning("quiz_picture_unavailable")
            return None

    def present(self, session_id, message, written, picture):
        """A program-written reply that shows the new question (and its picture link)."""
        output = reply(message, INTENT_PRACTICE, question(render_question(written["new_question"])))
        if picture:
            output["message"] += f"\n\n[{QUIZ_PICTURE_LINK}]({picture['url']})"
        validate(output, self.deps.response_schema)
        if picture:
            self.deps.artifacts.commit(session_id, picture)
        return output

    # ------------------------------------------------------------ check offer and “换一题”

    @staticmethod
    def offer_check(output, check, state, brief) -> bool:
        """End an explanation with an offer of one graded question on it; True when offered.

        A running practice set is left alone. The explanation is kept so the
        question, asked only if the learner takes the offer, tests exactly it.
        """
        running = state.get(Key.QUIZ) or {}
        if running and not running.get("finished") and running.get("kind", QuizKind.PRACTICE) != QuizKind.CHECK:
            return False
        state[Key.CHECK_OFFER] = {"topic": check["topic"], "difficulty": check["difficulty"],
                                  "explanation": output["message"][:OFFER_EXPLANATION_CHARS],
                                  "participant": brief.get("participant") or default_participant(state)}
        output["next_step"] = question(CHECK_OFFER)
        return True

    async def ask_check(self, turn, offer):
        """The learner took the check offer: one graded question on the explanation it came with."""
        state = turn.state
        quiz = new_quiz(kind=QuizKind.CHECK, topic=offer["topic"], total=CHECK_QUESTIONS, difficulty=offer["difficulty"],
                        participant=offer["participant"], history=recent_questions(state.get(Key.QUIZ)))
        memory = read_memory(state, self.deps.store, turn.memory_key).snapshot
        context = {"explanation": offer["explanation"], **self.request_context(turn, memory, offer["participant"])}
        try:
            written, picture = await self.write_question(quiz["participant"], quiz, context,
                                                         session_id=turn.session_id)
        except (StageFailure, ContractError):
            LOG.warning("check_question_unavailable")
            return reply("这次没能出好题。刚才哪里不清楚，可以直接问我。", INTENT_CHAT,
                         question("接下来想了解什么？")), state
        state[Key.QUIZ] = quiz
        output = self.present(turn.session_id, "好，来一道小题，只考刚才讲的那一块。", written, picture)
        state[Key.LAST_EXECUTION] = {"mode": Mode.CHECK_QUESTION, "tools": empty_results()}
        return output, state

    @staticmethod
    def decline_check(turn):
        return reply("好，这一块先不检验。还有哪里不清楚，随时问我。", INTENT_CHAT,
                     question("接下来想了解什么？")), turn.state

    async def replace_question(self, turn):
        """“换一题”: drop the waiting question without grading and ask a new one."""
        state = turn.state
        before = deepcopy(state.get(Key.QUIZ))
        quiz = state[Key.QUIZ]
        quiz["awaiting"] = None
        quiz["skipped"] = quiz.get("skipped", 0) + 1
        memory = read_memory(state, self.deps.store, turn.memory_key).snapshot
        try:
            written, picture = await self.write_question(
                quiz.get("participant", Participant.UNKNOWN), quiz,
                self.request_context(turn, memory, quiz.get("participant")), session_id=turn.session_id)
        except (StageFailure, ContractError):
            LOG.warning("replacement_question_unavailable")
            state[Key.QUIZ] = before
            return reply("这次没能换题。可以先试着回答这道，或者说“停止练习”。", INTENT_PRACTICE,
                         question(render_question(before["awaiting"]))), state
        return self.present(turn.session_id, "好，这题不算分，换一道。", written, picture), state
