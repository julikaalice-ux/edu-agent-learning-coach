"""Only a validated final response crosses the Responses hosting boundary.

The learner-facing text is the wire response's ``message`` plus any ``next_step``
prompt (quiz question faces included). The full four-field wire stays in session
state (``learning_coach.last_response``) for evaluation and debugging; it is
never serialized as chat text.
"""

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
    request context, and get None, so memory falls back to the browser
    learner_id or session-scoped keys exactly as before.
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

        async def execute():
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
            output, state = await self.pipeline.run(
                inputs, session_id=active_session.session_id,
                learner_id=owner,
                state=state_arg,
            )
            active_session.state["learning_coach"] = {**state, "last_response": output}
            active_session.state[GUARD_KEY] = {"owner": owner, "last_turn_at": time.time(),
                                               "skip": guard.get("skip", 0)}
            return AgentResponse(messages=[Message("assistant", [Content.from_text(learner_text(output))])],
                                 agent_id=self.id)

        if not stream:
            return execute()

        async def updates():
            response = await execute()
            yield AgentResponseUpdate(role="assistant", contents=[Content.from_text(response.text)],
                                      agent_id=self.id, message_id=str(uuid4()))

        return ResponseStream(updates(), finalizer=AgentResponse.from_updates)
