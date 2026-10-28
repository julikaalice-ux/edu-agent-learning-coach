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
import hashlib
import logging
import json
import os
import time
from uuid import uuid4

from agent_framework import AgentResponse, AgentResponseUpdate, BaseAgent, Content, Message, ResponseStream

try:
    from azure.ai.agentserver.core import get_request_context
except ImportError:  # hosted-agent dependency only; Space and local demos don't carry it
    get_request_context = None

from learning_coach.service.execution import TURN_OUTCOME, TurnOutcome


def wire_text(text, outcome, *, preview=False):
    return json.dumps({"turn_protocol": 1, "type": "preview" if preview else "final",
                       "status": "in_progress" if preview else outcome.status,
                       "error_code": None if preview else outcome.error_code,
                       "text": text}, ensure_ascii=False) + "\n"


def history_text(text):
    """Strip program transport envelopes before replaying assistant history."""
    try:
        frames = [json.loads(line) for line in text.splitlines() if line.strip()]
        if frames and all(isinstance(f, dict) and f.get("turn_protocol") == 1 for f in frames):
            return frames[-1]["text"]
    except (ValueError, KeyError, TypeError):
        pass
    return text


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


def records_key(owner: str | None, session_id: str) -> str | None:
    """Whose learning records this turn reads and writes (None: the conversation's own).

    By default records belong to the conversation. Hosted visitors that reach
    the agent through one shared identity (a Space calling with its service
    principal) all carry the same platform user id, so keying by it alone
    would give every visitor the same records. The owner still takes part,
    so a different user opening the same conversation link starts empty.
    LEARNING_COACH_RECORDS_SCOPE=user keys records by the platform user id
    across conversations; use it only when every learner signs in as themselves.
    """
    if owner is None:
        return None
    if os.environ.get("LEARNING_COACH_RECORDS_SCOPE", "").strip().lower() == "user":
        return owner
    return "conversation:" + hashlib.sha256(f"{owner}\0{session_id}".encode()).hexdigest()[:32]


def platform_learner_id() -> str | None:
    """The platform-injected per-user key (x-agent-user-id), when present.

    Non-hosted runs (Space, local demos) have no agentserver package and no
    request context, and get None. records_key decides how it keys records.
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
            LOG.info("platform_learner_id present records_scope=%s",
                     os.environ.get("LEARNING_COACH_RECORDS_SCOPE", "").strip().lower() or "conversation")
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
        structured = os.environ.get("LEARNING_COACH_UI_PROTOCOL", "0") == "1"

        def prepare():
            """The pipeline's arguments for this turn, and the guard to save after it."""
            if isinstance(messages, str):
                inputs = [{"role": "user", "text": messages}]
            elif isinstance(messages, Message):
                inputs = [{"role": messages.role, "text": messages.text}]
            else:
                inputs = [{"role": message.role, "text": message.text} for message in (messages or [])]
            inputs = [{**m, "text": history_text(m["text"]) if m["role"] == "assistant" else m["text"]} for m in inputs]
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
            arguments = {"session_id": active_session.session_id,
                         "learner_id": records_key(owner, active_session.session_id), "state": state_arg}
            return inputs, arguments, (owner, guard.get("skip", 0))

        def commit(output, state, guard):
            owner, skip = guard
            active_session.state["learning_coach"] = {**state, "last_response": output}
            active_session.state[GUARD_KEY] = {"owner": owner, "last_turn_at": time.time(), "skip": skip}

        async def execute():
            inputs, arguments, guard = prepare()
            outcome = TurnOutcome()
            token = TURN_OUTCOME.set(outcome)
            try:
                output, state = await self.pipeline.run(inputs, **arguments)
            finally:
                TURN_OUTCOME.reset(token)
            commit(output, state, guard)
            text = learner_text(output)
            return AgentResponse(messages=[Message("assistant", [Content.from_text(
                wire_text(text, outcome) if structured else text)])], agent_id=self.id)

        if not stream:
            return execute()

        async def updates():
            """Reply text as it becomes known; together the pieces are what the learner reads.

            Draft tokens stay private. Checked previews may arrive early; the UI
            protocol finishes with an authoritative replacement and outcome.
            Legacy text clients receive only checked explanations and final text.
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
            outcome = TurnOutcome()
            token = TURN_OUTCOME.set(outcome)
            try:
                async with aclosing(run_stream(inputs, **arguments)) as events:
                    async for event in events:
                        if "output" in event:
                            commit(event["output"], event["state"], guard)
                            text = learner_text(event["output"])
                            piece = wire_text(text, outcome) if structured else continuation(shown, text)
                        elif "explanation" in event:
                            text = event["explanation"]
                            piece = wire_text(text, outcome, preview=True) if structured else continuation(shown, text)
                        else:
                            # Draft tokens may be invalid or superseded by a retry.
                            # Only checked explanations and the final transaction cross the API.
                            continue
                        if piece:
                            if not structured:
                                shown += piece
                            yield AgentResponseUpdate(role="assistant", contents=[Content.from_text(piece)],
                                                      agent_id=self.id, message_id=message_id)
            finally:
                TURN_OUTCOME.reset(token)

        return ResponseStream(updates(), finalizer=AgentResponse.from_updates)


def continuation(shown: str, text: str) -> str:
    """What to send so the learner ends up reading text after what was already shown."""
    if text.startswith(shown):
        return text[len(shown):]
    return "\n\n" + text
