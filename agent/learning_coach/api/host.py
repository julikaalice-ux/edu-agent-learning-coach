"""Only learner-facing text crosses the Responses hosting boundary.

The learner-facing text is the wire response's ``message`` plus any ``next_step``
prompt (quiz question faces included). When streamed, the reply text arrives
while the coordinator writes it, an early explanation as soon as it is
checked, and the rest once the whole reply is checked. The full four-field
wire stays in session state (``learning_coach.last_response``) for evaluation
and debugging; it is never serialized as chat text.
"""

from contextlib import aclosing
from copy import deepcopy
import logging
import os
import time
from uuid import uuid4

from agent_framework import AgentResponse, AgentResponseUpdate, BaseAgent, Content, Message, ResponseStream

try:
    from azure.ai.agentserver.core import get_request_context
except ImportError:  # hosted-agent dependency only; Space and local demos don't carry it
    get_request_context = None

LOG = logging.getLogger(__name__)
MAX_LEARNER_ID_CHARS = 200  # matches pipeline MAX_ID_CHARS
_platform_id_logged = False

# Context isolation: the platform replays a conversation's full history and
# restores its session state for whoever opens the link. A "context guard"
# (kept in its own session key, never in pipeline state) decides instead that
# the conversation was closed and reopened: owner changed, or the previous
# turn is older than the TTL. A fresh start keeps only the current message.
GUARD_KEY = "context_guard"


def _context_ttl_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("LEARNING_COACH_CONTEXT_TTL_SECONDS", "900")))
    except ValueError:
        return 900.0


def _fresh_start(guard: dict, owner: str | None, now: float, ttl: float) -> bool:
    if not guard:
        return True
    if guard.get("owner") != owner:
        # A different platform user on the same conversation link never sees
        # the previous owner's context. An owner that appears or disappears
        # (identity unknown) is treated as a new start too.
        return True
    last = guard.get("last_turn_at")
    if last is not None and ttl > 0 and now - last > ttl:
        return True
    return False


def platform_learner_id() -> str | None:
    """The platform-injected per-user key (x-agent-user-id), when present.

    Non-hosted runs (Space, local demos) have no agentserver package and no
    request context, and get None, so memory falls back to session-scoped keys.
    """
    global _platform_id_logged
    if get_request_context is None:
        return None
    try:
        user_id = get_request_context().user_id
    except Exception:
        return None
    if isinstance(user_id, str) and user_id.strip() and len(user_id.strip()) <= MAX_LEARNER_ID_CHARS:
        if not _platform_id_logged:
            LOG.info("platform_learner_id present: per-user memory keys enabled")
            _platform_id_logged = True
        return user_id.strip()
    return None


def learner_text(output):
    """Compose the learner-facing text: message plus any next-step prompt.

    Quiz question faces and follow-up questions live in ``next_step``; without
    appending them the learner would never see the question.
    """
    text = output["message"]
    next_step = output.get("next_step")
    if next_step:
        text = f"{text}\n\n{next_step['text']}"
    return text


class LearningCoachAgent(BaseAgent):
    def __init__(self, pipeline):
        super().__init__(name="learning-coach", description="Five-role learning coach with validated output")
        self.pipeline = pipeline

    def run(self, messages=None, *, stream=False, session=None, options=None,
            function_invocation_kwargs=None, client_kwargs=None, **kwargs):
        active_session = session if session is not None else self.create_session()

        def prepare():
            """The pipeline's arguments for this turn, and the guard to save after it."""
            if isinstance(messages, str):
                inputs = [{"role": "user", "text": messages}]
            elif isinstance(messages, Message):
                inputs = [{"role": messages.role, "text": messages.text}]
            else:
                inputs = [{"role": message.role, "text": message.text} for message in (messages or [])]
            original = inputs
            owner = platform_learner_id()
            guard = active_session.state.get(GUARD_KEY) or {}
            fresh = _fresh_start(guard, owner, time.time(), _context_ttl_seconds())
            if fresh:
                # The conversation was closed and reopened (or a different person
                # opened the link): drop the replayed history and the saved state,
                # and clear in-memory replays so nothing old can be handed back.
                # skip keeps every later turn of this page-open out of the old
                # thread: the platform replays the full history each time.
                guard = {"skip": max(0, len(original) - 1)}
                inputs = original[-1:]
                reset = getattr(self.pipeline, "reset_session", None)
                if reset is not None:
                    reset(active_session.session_id)
            else:
                inputs = original[guard.get("skip") or 0:]
                if not inputs and original:
                    # The platform truncates long threads from the front; when
                    # the pre-reset prefix is gone, keep only the current message.
                    inputs = original[-1:]
            state_arg = {} if fresh else deepcopy(active_session.state.get("learning_coach", {}))
            arguments = {"session_id": active_session.session_id, "learner_id": owner, "state": state_arg}
            return inputs, arguments, (owner, guard.get("skip", 0))

        def commit(output, state, guard):
            owner, skip = guard
            active_session.state["learning_coach"] = {**state, "last_response": output}
            active_session.state[GUARD_KEY] = {"owner": owner, "last_turn_at": time.time(), "skip": skip}

        async def execute():
            inputs, arguments, guard = prepare()
            output, state = await self.pipeline.run(inputs, **arguments)
            commit(output, state, guard)
            return AgentResponse(messages=[Message("assistant", [Content.from_text(learner_text(output))])],
                                 agent_id=self.id)

        if not stream:
            return execute()

        async def updates():
            """Reply text as it becomes known; together the pieces are what the learner reads.

            The Responses wire keeps every piece sent, so a piece is never taken
            back. The final reply normally continues what was shown; when it
            does not (a draft the program replaced, or a failed turn), it
            follows the shown text after a blank line.
            """
            message_id = str(uuid4())
            run_stream = getattr(self.pipeline, "run_stream", None)
            if run_stream is None:
                response = await execute()
                yield AgentResponseUpdate(role="assistant", contents=[Content.from_text(response.text)],
                                          agent_id=self.id, message_id=message_id)
                return
            inputs, arguments, guard = prepare()
            shown = ""
            async with aclosing(run_stream(inputs, **arguments)) as events:
                async for event in events:
                    if "output" in event:
                        commit(event["output"], event["state"], guard)
                        piece = continuation(shown, learner_text(event["output"]))
                    elif "explanation" in event:
                        piece = continuation(shown, event["explanation"])
                    else:
                        piece = event.get("delta") or ""
                    if piece:
                        shown += piece
                        yield AgentResponseUpdate(role="assistant", contents=[Content.from_text(piece)],
                                                  agent_id=self.id, message_id=message_id)

        return ResponseStream(updates(), finalizer=AgentResponse.from_updates)


def continuation(shown: str, text: str) -> str:
    """What to send so the learner ends up reading text after what was already shown."""
    if text.startswith(shown):
        return text[len(shown):]
    return "\n\n" + text
