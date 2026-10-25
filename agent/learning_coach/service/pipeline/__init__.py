"""One learner turn: the event shell, the program's judgement, then one handler.

    shell.py        checks, replays, one lock and one deadline; a failed turn changes nothing
    router.py       what this event is: record commands, then the phase table (no model)
    commands.py     record on/off/delete and the first-meeting questions
    teach.py        the teaching turn: coordinator decision, tools, reply
    quiz.py         quiz sets, grading, questions, the check offer, “换一题”
    notes.py        compiling learning notes
    permissions.py  external access: asking once, resuming the held turn
    checks.py       program checks on a reply before anyone sees it
    streaming.py    the coordinator's reply text shown while it is written
    state.py        the state keys and small reads/writes on them
    texts.py        fixed wording and the replies the program recognizes

The model proposes; the program decides what runs, grades answers, holds
permissions and decides what is saved.
"""

import asyncio
from contextlib import suppress

from learning_coach.domain.contracts import QUIZ_SCHEMA, decision_schema, validate
from learning_coach.domain.context import Budgets
from learning_coach.domain.note_contracts import NOTES_SCHEMA
from learning_coach.service.execution import EXPLANATION_SINK
from learning_coach.service.pipeline import permissions, router
from learning_coach.service.pipeline.checks import correct_choice
from learning_coach.service.pipeline.commands import Commands, last_forget
from learning_coach.service.pipeline.deps import Deps
from learning_coach.service.pipeline.notes import NotesService
from learning_coach.service.pipeline.quiz import PICTURE_TIMEOUT_SECONDS, QuizService
from learning_coach.service.pipeline.router import Action, Command
from learning_coach.service.pipeline.shell import TurnShell
from learning_coach.service.pipeline.state import Key, default_participant, empty_learning_notes
from learning_coach.service.pipeline.teach import Teacher
from learning_coach.service.pipeline.texts import (CHECK_OFFER, NOTES_OFFER, QUIZ_PICTURE_LINK, render_question,
                                                   reply)
from learning_coach.service.ports import (AgentRunner, ResearchPort, LearningMemory, Artifacts, ImageFinder,
                                          ImageGenerator, Role)

__all__ = ["LearningPipeline", "CHECK_OFFER", "NOTES_OFFER", "QUIZ_PICTURE_LINK", "PICTURE_TIMEOUT_SECONDS",
           "correct_choice", "default_participant", "empty_learning_notes", "last_forget", "render_question",
           "reply"]

def _shared(name):
    """An attribute kept in the shared Deps, so a replacement reaches every handler."""
    return property(lambda self: getattr(self.deps, name), lambda self, value: setattr(self.deps, name, value))


class LearningPipeline:
    runner, research, store, artifacts = (_shared("runner"), _shared("research"), _shared("store"),
                                          _shared("artifacts"))
    image_generator, image_finder = _shared("image_generator"), _shared("image_finder")
    budgets, response_schema = _shared("budgets"), _shared("response_schema")

    def __init__(self, runner: AgentRunner, research: ResearchPort, store: LearningMemory,
                 artifacts: Artifacts, response_schema: dict, *,
                 image_generator: ImageGenerator | None = None,
                 image_finder: ImageFinder | None = None,
                 onboarding: bool = False, demo_stage: str | None = None,
                 budgets: Budgets | None = None):
        # Token budgets for what each agent sees (domain/context.py). The image
        # backends are callables injected by server.py or the tests; None turns
        # them off (image_finder finds web pictures for gallery pages).
        self.deps = Deps(runner, research, store, artifacts, image_generator, image_finder,
                         budgets or Budgets(), response_schema)
        self.decision_schema = decision_schema(response_schema)

        self.shell = TurnShell(response_schema)
        self.quiz = QuizService(self.deps, draft=lambda request: self._draft_question(request))
        self.notes = NotesService(self.deps, draft=lambda payload: self._draft_notes(payload))
        self.commands = Commands(self.deps, shell=self.shell, ask_first=onboarding, demo_stage=demo_stage)
        self.teacher = Teacher(self.deps, quiz=self.quiz, decision_schema=self.decision_schema)

    # First-meeting settings live with the commands that use them.
    onboarding = property(lambda self: self.commands.ask_first,
                          lambda self, value: setattr(self.commands, "ask_first", value))
    demo_stage = property(lambda self: self.commands.demo_stage,
                          lambda self, value: setattr(self.commands, "demo_stage", value))

    # The shell's replay caches, by their historical names.
    @property
    def _recent(self):
        return self.shell.recent

    @property
    def _accepted(self):
        return self.shell.accepted

    # ------------------------------------------------------------ entry points

    async def run(self, messages, *, session_id, state=None, turn_id=None,
                  learner_id=None, returning_visitor=False, on_delta=None, on_explanation=None,
                  external_permissions=None):
        """Run one turn.

        session_id identifies this conversation. learner_id is an optional,
        anonymous ID that the client keeps across conversations (for example in
        the browser); learning records are stored under it so a returning
        learner keeps their progress. Without it, records belong to the
        conversation only. returning_visitor is a client hint that this device
        has talked to the coach before; it only suppresses the first-meeting
        introduction and grants nothing else.
        """
        return await self.shell.run(self._handle, messages, session_id=session_id, state=state, turn_id=turn_id,
                                     learner_id=learner_id, returning_visitor=returning_visitor,
                                     on_delta=on_delta, on_explanation=on_explanation,
                                     external_permissions=external_permissions)

    async def run_stream(self, messages, **kwargs):
        """Reply text as it becomes known, then one final response/state transaction.

        Events, in order:
          {"delta": text}        reply text while the coordinator writes it (a draft;
                                 the final reply normally continues it)
          {"explanation": text}  a checked explanation shown before the visual is ready;
                                 the final reply starts with it
          {"output": ..., "state": ...}  the checked reply, always last

        Closing this iterator cancels and joins the turn; no orphan worker can
        publish a late visual after the host has reset the conversation.
        """
        events = asyncio.Queue()
        callback = kwargs.pop("on_explanation", None)
        delta_callback = kwargs.pop("on_delta", None)

        def publish(text):
            if callback is not None:
                callback(text)
            events.put_nowait({"explanation": text})

        def publish_delta(text):
            if delta_callback is not None:
                delta_callback(text)
            events.put_nowait({"delta": text})

        task = asyncio.create_task(self.run(messages, on_explanation=publish, on_delta=publish_delta, **kwargs))
        getter = None
        try:
            while not task.done() or not events.empty():
                if not events.empty():
                    yield events.get_nowait()
                    continue
                getter = asyncio.create_task(events.get())
                done, _ = await asyncio.wait((task, getter), return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    getter = None
                else:
                    getter.cancel()
                    with suppress(asyncio.CancelledError):
                        await getter
                    getter = None
            output, state = await task
            yield {"output": output, "state": state}
        finally:
            if getter is not None:
                getter.cancel()
                with suppress(asyncio.CancelledError):
                    await getter
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def reset_session(self, session_id: str) -> None:
        """Drop this conversation's in-memory replays so a reopened page starts clean.

        The host calls this when it decides the conversation was closed and
        reopened: without it, the shell would hand the fresh turn the old state
        (its revision is ahead of the empty one) and could replay an old response.
        """
        self.shell.reset(session_id)

    # ------------------------------------------------------------ one turn

    async def _handle(self, turn):
        """The program judges what this event is; then exactly one handler runs."""
        from learning_coach.service.pipeline import learning
        if turn.text.startswith(learning.PREFIX):
            return await learning.handle(self, turn)
        state = turn.state
        # An interrupt first: an answer to the external-access question resumes the held turn.
        permissions.resume_held_turn(turn)
        # Offers and the delete question last exactly one turn.
        offers = {key: state.pop(key, None) for key in (Key.NOTES_OFFER, Key.CHECK_OFFER)}
        delete_asked = state.pop(Key.PENDING_FORGET, False)

        command = router.record_command(turn.text, delete_asked)
        if command is not None:
            return self._command(command, turn)
        # Visible history before the latest confirmed deletion is not used again.
        forgotten = last_forget(turn.messages)
        if forgotten >= 0:
            turn.messages = turn.messages[forgotten + 1:]
        answered = self.commands.first_meeting(turn)
        if answered is not None:
            return answered, state
        self.commands.introduce(turn)
        if turn.prefix is not None:
            # The first question is taught now, after the program's own words.
            return await self._teach_with_prefix(turn)

        action = router.next_action(turn.text, router.phases(state, offers))
        if action is Action.COMPILE_NOTES:
            return await self.notes.compile(turn)
        if action is Action.DECLINE_NOTES:
            return self.notes.decline(turn)
        if action is Action.SHOW_MISTAKES:
            return self.notes.show_mistakes(turn)
        if action is Action.ASK_CHECK:
            return await self.quiz.ask_check(turn, offers[Key.CHECK_OFFER])
        if action is Action.DECLINE_CHECK:
            return self.quiz.decline_check(turn)
        if action is Action.REPLACE_QUESTION:
            return await self.quiz.replace_question(turn)
        return await self._teach(turn)

    def _command(self, command, turn):
        if command is Command.ASK_DELETE:
            return self.commands.ask_delete(turn)
        if command is Command.FORGET:
            return self.commands.forget(turn)
        return self.commands.set_records(turn, command is Command.RECORDS_ON)

    async def _teach_with_prefix(self, turn):
        """Teach with the program's words (first-meeting closing or introduction) in front."""
        prefix = turn.prefix
        if turn.on_delta is not None:
            # The program's own words are final: shown at once, before any
            # model call, so a learner waiting for a visual is not left blank.
            turn.on_delta(prefix + "\n\n")
        sink = EXPLANATION_SINK.get()
        token = None
        if sink is not None:
            # The closing sentence is part of the same immutable prefix,
            # including when a later stage fails after early delivery.
            token = EXPLANATION_SINK.set(lambda output: sink({**output, "message": prefix + "\n\n" + output["message"]}))
        try:
            output, state = await self._teach(turn)
        finally:
            if token is not None:
                EXPLANATION_SINK.reset(token)
        output["message"] = prefix + "\n\n" + output["message"]
        validate(output, self.response_schema)
        return output, state

    # ------------------------------------------------------------ what the single-agent baseline replaces

    async def _teach(self, turn):
        return await self.teacher.run(turn)

    async def _draft_notes(self, payload):
        """The notes agent writes the draft; the program checks and writes it."""
        return await self.runner(Role.NOTES, payload, NOTES_SCHEMA)

    async def _draft_question(self, request):
        """The quiz writer drafts one question; the program checks it."""
        return await self.runner(Role.QUIZZER, request, QUIZ_SCHEMA)
