"""Application-owned ports. Adapters conform structurally; no SDK imports."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable, Protocol


class StorageError(RuntimeError):
    """An adapter could not read or write storage; contains no private details."""


class Role(StrEnum):
    """The five agents an AgentRunner serves."""
    COORDINATOR = "coordinator"
    VERIFIER = "verifier"
    NOTES = "notes"
    VISUALIZER = "visualizer"
    QUIZZER = "quizzer"


class CallPhase(StrEnum):
    """input.phase of a request, where a role has more than one job."""
    DECIDE = "decide"
    RESPOND = "respond"
    QUIZ = "quiz"
    VISUAL_REVIEW = "visual_review"


class AgentRunner(Protocol):
    # images: optional [(label, bytes, media_type)] the role should look at.
    async def __call__(self, role: str, payload: dict, schema: dict, images: list | None = None) -> dict: ...


class ResearchPort(Protocol):
    async def run(self, question: str, queries: list[str] | None = None) -> dict: ...


class ImageGenerator(Protocol):
    async def __call__(self, prompt: str) -> bytes: ...


class ImageFinder(Protocol):
    """Finds existing pictures on the web for a gallery panel.

    Returns at most ``limit`` candidates, best first, each
    {source, title, description, author, license, page_url, data, media_type};
    ``data`` is the downloaded picture. Raises nothing for "no results".
    """
    async def find(self, query_zh: str, query_en: str, *, limit: int = 3) -> list[dict]: ...


class LearningMemory(Protocol):
    def learning_view(self, session_id: str) -> dict: ...
    def edit_study_note(self, session_id: str, card_id: str, fields: dict) -> None: ...
    def retry_card(self, session_id: str, card_id: str, event_id: str, choice=None, reveal=False) -> dict: ...
    def is_enabled(self, session_id: str) -> bool: ...
    def set_enabled(self, session_id: str, enabled: bool) -> None: ...
    def snapshot(self, session_id: str) -> dict: ...
    def clear(self, session_id: str) -> None: ...
    def apply_notes(self, session_id: str, turn_id: str, notes: dict,
                    learner_text: str, quiz_evidence: dict | None = None) -> bool: ...


class Artifacts(Protocol):
    # photos: per gallery panel, the chosen picture {data_url, title, author, license, page_url, source} or None.
    def prepare(self, session_id: str, spec: dict, image: bytes | None = None,
                photos: list | None = None) -> dict: ...
    def commit(self, session_id: str, artifact: dict) -> None: ...
    def get(self, token: str) -> str | None: ...
    def delete_session(self, session_id: str) -> None: ...


class SearchProvider(Protocol):
    async def search(self, query: str) -> list[dict]: ...


class VisualRenderer(Protocol):
    def __call__(self, spec: dict, image: bytes | None = None, photos: list | None = None) -> str: ...


@dataclass(frozen=True)
class ToolSpec:
    """A program function the single-agent baseline may call; adapters turn it into a model tool."""
    name: str
    description: str
    parameters: dict          # JSON schema of the keyword arguments
    func: Callable[..., Awaitable[dict]]


class SingleAgentPort(Protocol):
    """One model with tools; returns the final JSON already checked against schema."""
    async def __call__(self, task: str, payload: dict, schema: dict, tools: list[ToolSpec]) -> dict: ...
