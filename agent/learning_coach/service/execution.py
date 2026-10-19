"""Application execution policy shared by orchestration and model adapters."""

from contextvars import ContextVar
from dataclasses import dataclass


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
EXTERNAL_CAPABILITIES = frozenset({"research", "image_search", "image_generation", "mcp"})
EXTERNAL_PERMISSIONS = ContextVar("learning_coach_external_permissions", default=frozenset())
EXTERNAL_DECLINED = ContextVar("learning_coach_external_declined", default=False)
EXPLANATION_SINK = ContextVar("learning_coach_explanation_sink", default=None)


def external_allowed(capability: str) -> bool:
    return capability in EXTERNAL_CAPABILITIES and capability in EXTERNAL_PERMISSIONS.get()


class StageFailure(RuntimeError):
    """A bounded model stage could not produce an accepted result."""
