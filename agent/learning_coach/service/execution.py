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


class StageFailure(RuntimeError):
    """A bounded model stage could not produce an accepted result."""
