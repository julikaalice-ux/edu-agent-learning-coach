"""The event shell every turn runs in: checks, replays, one lock, one deadline, all or nothing.

The handlers inside a turn never deal with transports. The shell
- checks the IDs and the external permissions the host granted,
- keeps the visible history (cleaned and capped),
- answers a transport replay with the response it already accepted,
- runs one turn at a time per session, under one deadline,
- and on any failure returns a fixed reply with the state from before the
  turn, so a failed turn never commits half of its work.
"""

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Callable

from learning_coach.domain.contracts import ContractError, validate
from learning_coach.service.execution import (BUDGET, CallBudget, StageFailure, EXTERNAL_CAPABILITIES,
                                              EXTERNAL_PERMISSIONS, EXTERNAL_DECLINED, EXPLANATION_SINK)
from learning_coach.service.pipeline.state import Key
from learning_coach.service.pipeline.texts import question, reply
from learning_coach.service.ports import StorageError

LOG = logging.getLogger(__name__)

# Visible history accepted per turn (notes are compiled from the learner's
# words since the last compile); a safety cap only, not a context budget.
MAX_HISTORY = 400
MAX_MESSAGE_CHARS = 20000
MAX_ID_CHARS = 200              # session and learner IDs
TURN_TIMEOUT_SECONDS = 180      # a whole turn, all agents included
REPLAY_CACHE_SIZE = 2048        # accepted turns kept for transport replays
SESSION_CACHE_SIZE = 512        # recent session states kept in memory

USER, ASSISTANT = "user", "assistant"   # the two message roles kept in the history
TURN_FAILED = "这次处理没有顺利完成，学习记录和进度没有继续推进。"
# Errors a turn may end with; anything else is a bug and propagates.
TURN_ERRORS = (StageFailure, ContractError, TimeoutError, OSError, ValueError, StorageError)


@dataclass
class Turn:
    """One learner event, as the handlers see it.

    messages and state are this turn's working copies; handlers may replace
    messages (a held turn resumed, the question asked before onboarding).
    """
    messages: list
    state: dict
    session_id: str
    event_id: str
    memory_key: str
    returning_visitor: bool = False
    on_delta: Callable | None = None
    # Onboarding's closing words, put in front of this turn's reply.
    prefix: str | None = None

    @property
    def text(self) -> str:
        return self.messages[-1]["text"].strip()


def clean_history(messages) -> list:
    return [{"role": m["role"], "text": m["text"][:MAX_MESSAGE_CHARS]} for m in messages[-MAX_HISTORY:]
            if m.get("role") in (USER, ASSISTANT) and isinstance(m.get("text"), str)]


class TurnShell:
    def __init__(self, response_schema: dict):
        self.response_schema = response_schema
        self.locks = {}
        self.recent = OrderedDict()     # session_id -> latest accepted state
        self.accepted = OrderedDict()   # (session_id, event_id) -> {"input", "response"}

    async def run(self, handle, messages, *, session_id, state=None, turn_id=None, learner_id=None,
                  returning_visitor=False, on_delta=None, on_explanation=None, external_permissions=None):
        """Check the event, then run handle(turn) -> (output, state) as one transaction."""
        # IDs are supplied by the host/application, never by model output.
        if not isinstance(session_id, str) or not session_id or len(session_id) > MAX_ID_CHARS:
            raise ValueError("invalid_session_id")
        if learner_id is not None and (not isinstance(learner_id, str) or not learner_id.strip()
                                       or len(learner_id) > MAX_ID_CHARS):
            raise ValueError("invalid_learner_id")
        grants = frozenset(EXTERNAL_PERMISSIONS.get() if external_permissions is None else external_permissions)
        if not grants <= EXTERNAL_CAPABILITIES:
            raise ValueError("unknown_external_permission")
        clean = clean_history(messages)
        if not clean or clean[-1]["role"] != USER or not clean[-1]["text"].strip():
            return reply("请告诉我你现在想学什么。", step=question("你想从哪个问题开始？")), deepcopy(state or {})
        # A history fingerprint distinguishes a transport replay from a real later answer.
        fingerprint = hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        event_id = turn_id or fingerprint
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            working = self._load(state, session_id)
            replayed = self._replay(working, session_id, event_id, fingerprint)
            if replayed is not None:
                return replayed, working
            baseline = deepcopy(working)
            turn = Turn(clean, working, session_id, event_id, learner_id or session_id,
                        returning_visitor is True, on_delta)
            try:
                output, updated = await self._transaction(handle, turn, grants, on_explanation)
            except _TurnFailed as failed:
                return failed.output, baseline
            validate(output, self.response_schema)
            self._accept(session_id, event_id, fingerprint, output, updated, working.get(Key.REVISION, 0))
            # One application event from the accepted public response. No raw learner text.
            LOG.info("intent_result %s", json.dumps({"turn": hashlib.sha256(event_id.encode()).hexdigest(),
                                                     "intent": output["intent"]}, ensure_ascii=False))
            return output, updated

    def _load(self, state, session_id):
        working = deepcopy(state or {})
        if working.get(Key.OWNER, session_id) != session_id:
            raise ValueError("session_state_owner_mismatch")
        cached = self.recent.get(session_id)
        if cached and cached.get(Key.REVISION, 0) >= working.get(Key.REVISION, 0):
            working = deepcopy(cached)
        return working

    def _replay(self, working, session_id, event_id, fingerprint):
        """The response already accepted for this event, or None for a new event."""
        accepted = self.accepted.get((session_id, event_id))
        if accepted is not None:
            if accepted["input"] != fingerprint:
                raise ValueError("turn_id_conflict")
            return deepcopy(accepted["response"])
        if working.get(Key.LAST_EVENT) == event_id and working.get(Key.LAST_INPUT) == fingerprint:
            return deepcopy(working[Key.LAST_RESPONSE])
        if working.get(Key.LAST_EVENT) == event_id:
            raise ValueError("turn_id_conflict")
        return None

    async def _transaction(self, handle, turn, grants, on_explanation):
        """Per-turn budget, permissions and early-explanation sink around one deadline."""
        tokens = [(BUDGET, BUDGET.set(CallBudget())), (EXTERNAL_PERMISSIONS, EXTERNAL_PERMISSIONS.set(grants)),
                  (EXTERNAL_DECLINED, EXTERNAL_DECLINED.set(False))]
        delivered = []

        def deliver(explanation):
            # Only a complete validated block reaches this sink, never raw JSON/tokens.
            if not delivered:
                delivered.append(deepcopy(explanation))
                if on_explanation is not None:
                    on_explanation(explanation["message"])

        tokens.append((EXPLANATION_SINK, EXPLANATION_SINK.set(deliver)))
        try:
            return await asyncio.wait_for(handle(turn), timeout=TURN_TIMEOUT_SECONDS)
        except TURN_ERRORS:
            LOG.warning("learning_turn_failed")
            output = reply(TURN_FAILED, step=question("要从刚才的问题再试一次吗？"))
            if delivered:
                output = deepcopy(delivered[0])
                output["message"] += "\n\n后续图示未能完成，先保留以上讲解。学习进度没有继续推进。"
                output["next_step"] = question("想重试图示，还是继续文字讲解？")
            raise _TurnFailed(output) from None
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    def _accept(self, session_id, event_id, fingerprint, output, updated, revision):
        updated.update({Key.LAST_EVENT: event_id, Key.LAST_INPUT: fingerprint, Key.LAST_RESPONSE: deepcopy(output),
                        Key.REVISION: revision + 1, Key.OWNER: session_id})
        self.recent[session_id] = deepcopy(updated)
        self.accepted[(session_id, event_id)] = {"input": fingerprint, "response": deepcopy(output)}
        while len(self.accepted) > REPLAY_CACHE_SIZE:
            self.accepted.popitem(last=False)
        self.recent.move_to_end(session_id)
        while len(self.recent) > SESSION_CACHE_SIZE:
            old_id, _ = self.recent.popitem(last=False)
            old_lock = self.locks.get(old_id)
            if old_lock is not None and not old_lock.locked():
                self.locks.pop(old_id, None)

    def forget_replays(self, session_id):
        """Accepted responses of this session can no longer be replayed."""
        for key in [key for key in self.accepted if key[0] == session_id]:
            del self.accepted[key]

    def reset(self, session_id):
        """Drop this conversation's in-memory replays so a reopened page starts clean."""
        self.recent.pop(session_id, None)
        self.forget_replays(session_id)


class _TurnFailed(Exception):
    def __init__(self, output):
        super().__init__("turn_failed")
        self.output = output
