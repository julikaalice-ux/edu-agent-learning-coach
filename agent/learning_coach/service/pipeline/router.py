"""What this event is, decided by the program from the state and the learner's exact words.

No model is involved here. Anything this module does not recognize is a
teaching turn, where the coordinator decides what was meant (a new topic,
an answer, wanting to stop, ...).

Two decisions, in this order:
1. record_command: record on / off / delete, and confirming a deletion.
   They apply in any phase.
2. next_action: the phase table. The phase is where the lesson is: a quiz
   question may be waiting, and an offer (a check question, notes) made in
   the previous reply lasts exactly one turn on top of that.
"""

from enum import StrEnum

from learning_coach.service.pipeline.state import Key, waiting_question
from learning_coach.service.pipeline.texts import (CHECK_NO, CHECK_YES, CMD_FORGET, CMD_MEMORY_OFF, CMD_MEMORY_ON,
                                                   CONFIRM_DELETE, MISTAKES_REQUEST, NOTES_NO, NOTES_REQUEST, NOTES_YES,
                                                   RECORD_DELETE, RECORD_OFF, RECORD_ON, SKIP_QUESTION)


class Command(StrEnum):
    RECORDS_ON = "records_on"
    RECORDS_OFF = "records_off"
    ASK_DELETE = "ask_delete"
    FORGET = "forget"


class Phase(StrEnum):
    ANY = "any"
    OFFER_NOTES = "offer_notes"   # the last reply offered to compile notes
    OFFER_CHECK = "offer_check"   # the last reply offered a check question
    QUIZ = "quiz"                 # a question waits for an answer
    EXPLAIN = "explain"


class Action(StrEnum):
    TEACH = "teach"
    COMPILE_NOTES = "compile_notes"
    SHOW_MISTAKES = "show_mistakes"
    DECLINE_NOTES = "decline_notes"
    ASK_CHECK = "ask_check"
    DECLINE_CHECK = "decline_check"
    REPLACE_QUESTION = "replace_question"


# (phase, the learner's exact reply, action); the first row that applies wins.
# Every other combination is a teaching turn.
PHASE_TABLE = (
    (Phase.ANY, NOTES_REQUEST, Action.COMPILE_NOTES),
    (Phase.ANY, MISTAKES_REQUEST, Action.SHOW_MISTAKES),
    (Phase.OFFER_NOTES, NOTES_YES, Action.COMPILE_NOTES),
    (Phase.OFFER_NOTES, NOTES_NO, Action.DECLINE_NOTES),
    (Phase.OFFER_CHECK, CHECK_YES, Action.ASK_CHECK),
    (Phase.OFFER_CHECK, CHECK_NO, Action.DECLINE_CHECK),
    (Phase.QUIZ, SKIP_QUESTION, Action.REPLACE_QUESTION),
)


def record_command(text: str, delete_asked: bool) -> Command | None:
    if text in RECORD_ON or text == CMD_MEMORY_ON:
        return Command.RECORDS_ON
    if text in RECORD_OFF or text == CMD_MEMORY_OFF:
        return Command.RECORDS_OFF
    if text in RECORD_DELETE:
        return Command.ASK_DELETE
    if text == CMD_FORGET or (text == CONFIRM_DELETE and delete_asked):
        return Command.FORGET
    return None


def phases(state, offers) -> set:
    """The phases this turn is in: offers made last turn, then where the lesson stands."""
    found = {Phase.ANY}
    if offers.get(Key.NOTES_OFFER):
        found.add(Phase.OFFER_NOTES)
    if offers.get(Key.CHECK_OFFER):
        found.add(Phase.OFFER_CHECK)
    found.add(Phase.QUIZ if waiting_question(state) else Phase.EXPLAIN)
    return found


def next_action(text: str, current: set) -> Action:
    for phase, words, action in PHASE_TABLE:
        if phase in current and text in words:
            return action
    return Action.TEACH
