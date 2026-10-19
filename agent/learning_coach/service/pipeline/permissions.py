"""External access: what is available and authorized, asking once, and resuming the held turn.

Model plans and backend availability never grant access. Permission comes
from the host for the whole turn, or from the learner answering the
program's question; the turn that needed it is held in state meanwhile.
"""

from copy import deepcopy

from learning_coach.domain.routing import INTENT_CONCEPT
from learning_coach.service.execution import (EXTERNAL_CAPABILITIES, EXTERNAL_DECLINED, EXTERNAL_PERMISSIONS,
                                              Capability, external_allowed)
from learning_coach.service.pipeline.state import Key
from learning_coach.service.pipeline.texts import (EXTERNAL_LABELS, EXTERNAL_NO, EXTERNAL_QUESTION, EXTERNAL_YES,
                                                   question, reply)

PURPOSE_CHARS = 400


def capabilities(*, research, image_finder, image_generator) -> dict:
    available = {Capability.RESEARCH: research is not None, Capability.IMAGE_SEARCH: image_finder is not None,
                 Capability.IMAGE_GENERATION: image_generator is not None, Capability.MCP: False}
    return {key: {"available": value, "authorized": value and external_allowed(key)}
            for key, value in available.items()}


def ask(turn, state, wanted, purpose, primary=INTENT_CONCEPT):
    """Hold this turn and ask for one-time access to the wanted capabilities."""
    # Kept only in host-owned session state, excluded from model context.
    wanted = sorted(set(wanted) & EXTERNAL_CAPABILITIES)
    state[Key.PENDING_EXTERNAL] = {"messages": deepcopy(turn.messages), "capabilities": wanted,
                                   "purpose": purpose[:PURPOSE_CHARS], "granted": sorted(EXTERNAL_PERMISSIONS.get())}
    actions = "、".join(EXTERNAL_LABELS[key] for key in wanted)
    message = f"为了继续解释这个问题，需要{actions}。外部服务会收到与本题相关的查询词或图示描述；授权仅用于这次请求。"
    return reply(message, primary, question(EXTERNAL_QUESTION)), state


def resume_held_turn(turn):
    """An answer to the access question resumes the held turn; anything else drops it."""
    held = turn.state.pop(Key.PENDING_EXTERNAL, None)
    if not held or turn.text not in EXTERNAL_YES + EXTERNAL_NO:
        return
    granted = frozenset(held.get("granted", ()))
    if turn.text in EXTERNAL_YES:
        EXTERNAL_PERMISSIONS.set((granted | frozenset(held["capabilities"])) & EXTERNAL_CAPABILITIES)
    else:
        EXTERNAL_PERMISSIONS.set(granted & EXTERNAL_CAPABILITIES)
        EXTERNAL_DECLINED.set(True)
    turn.messages = deepcopy(held["messages"])
