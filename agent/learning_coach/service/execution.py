"""Application execution policy shared by orchestration and model adapters."""

from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum


DEFAULT_CALL_BUDGET = 12  # model calls allowed per turn; pipeline.py sets it per request


@dataclass
class CallBudget:
    remaining: int = DEFAULT_CALL_BUDGET


BUDGET = ContextVar("learning_coach_budget", default=None)
# Optional concurrent workers inherit the reservation; the final responder does
# not. The runner checks it before every real model request, including retries.
CALL_RESERVE = ContextVar("learning_coach_call_reserve", default=0)


# Per-turn authorization is supplied by the host or a program-owned permission
# question. Model plans and backend availability never grant permission.
class Capability(StrEnum):
    RESEARCH = "research"
    IMAGE_SEARCH = "image_search"
    IMAGE_GENERATION = "image_generation"
    MCP = "mcp"


EXTERNAL_CAPABILITIES = frozenset(Capability)
EXTERNAL_PERMISSIONS = ContextVar("learning_coach_external_permissions", default=frozenset())
EXTERNAL_DECLINED = ContextVar("learning_coach_external_declined", default=False)
EXPLANATION_SINK = ContextVar("learning_coach_explanation_sink", default=None)
# Set only around a coordinator call whose reply may be shown while it is
# written. The runner then streams that call and passes it the raw text
# written so far, once per piece; a retry starts again from empty text.
REPLY_SINK = ContextVar("learning_coach_reply_sink", default=None)


def external_allowed(capability: str) -> bool:
    return capability in EXTERNAL_CAPABILITIES and capability in EXTERNAL_PERMISSIONS.get()


class StageFailure(RuntimeError):
    """A bounded model stage could not produce an accepted result."""


def failure_codes(error):
    """Safe diagnostics for concurrent failures; never log exception messages.

    Only our own contract/stage errors can supply a short application code.
    Provider errors contribute their type, not their payload or credentials.
    """
    import re
    from learning_coach.domain.contracts import ContractError
    if isinstance(error, BaseExceptionGroup):
        return ",".join(failure_codes(child) for child in error.exceptions[:8])
    code = str(error) if isinstance(error, (ContractError, StageFailure)) else ""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        code = "unclassified"
    return f"{type(error).__name__}:{code}"
