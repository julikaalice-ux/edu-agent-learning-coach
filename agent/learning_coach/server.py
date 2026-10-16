"""Shared composition root for hosted API and Gradio entry points.

Only this module chooses the concrete model, storage and search adapters.
Importing it does not create clients, databases, or agents.
"""

import json
import os
from pathlib import Path

from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from learning_coach.db.memory import LearningStore
from learning_coach.domain.context import Budgets, encoder
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.research import ResearchService
from learning_coach.adapters.search_providers import default_provider
from learning_coach.adapters.runner import ROLES, StructuredRunner, create_agents
from learning_coach.adapters.visuals import render_visual
from learning_coach.db.artifacts import ArtifactStore

SRC_DIR = Path(__file__).resolve().parent.parent
INSTRUCTIONS_PATH = SRC_DIR / "prompts/instructions.md"


def demo_stage_from_env():
    """LEARNING_COACH_DEMO_MODE=1 skips the first-meeting questions (demonstrations only)."""
    if os.environ.get("LEARNING_COACH_DEMO_MODE", "0") != "1":
        return None
    return os.environ.get("LEARNING_COACH_DEMO_STAGE", "高中")


def role_models_from_env(default: str) -> dict[str, str]:
    """LEARNING_COACH_MODEL_<ROLE> picks a deployment per agent, e.g.
    LEARNING_COACH_MODEL_COORDINATOR=gpt-6-luna; unset roles use the default."""
    return {role: os.environ.get(f"LEARNING_COACH_MODEL_{role.upper()}", "").strip() or default
            for role in ROLES}


def _foundry_clients(endpoint: str, models: dict[str, str]) -> tuple[dict[str, FoundryChatClient], object]:
    """One client per distinct deployment, shared by the roles that use it.

    Also returns the shared credential so other platform-backed adapters
    (Foundry Memory) reuse it instead of building their own.
    """
    credential = DefaultAzureCredential()
    by_model = {model: FoundryChatClient(project_endpoint=endpoint, model=model, credential=credential)
                for model in set(models.values())}
    return {role: by_model[model] for role, model in models.items()}, credential


def _memory_store(directory: Path, credential):
    """SQLite only, or SQLite plus the Foundry Memory semantic mirror.

    LEARNING_COACH_MEMORY_STORE unset = pure SQLite (current behavior). When
    set, notes are mirrored into the named store and snapshot() merges
    semantic recalls; Foundry failures always degrade to SQLite behavior.
    """
    inner = LearningStore(directory / "memory.sqlite3")
    name = os.environ.get("LEARNING_COACH_MEMORY_STORE", "").strip()
    if not name:
        return inner
    from azure.ai.projects import AIProjectClient
    from learning_coach.adapters.foundry_memory import FoundryMemoryStore
    timeout = float(os.environ.get("LEARNING_COACH_MEMORY_TIMEOUT", "5"))
    search_max = int(os.environ.get("LEARNING_COACH_MEMORY_SEARCH_MAX", "6"))
    client = AIProjectClient(os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential, allow_preview=True,
                             connection_timeout=timeout, read_timeout=timeout, retry_total=0)
    # The adapter's client seam is the memory-store operations surface, not the
    # whole project client (create_memory lives under beta.memory_stores).
    return FoundryMemoryStore(inner, client.beta.memory_stores, name, search_max=search_max)


def create_pipeline(*, client=None, data_dir=None, public_base_url=None, native_json=None,
                    image_generator=None, search_provider=None):
    """Build dependencies without launching a server or contacting a model."""
    role_clients = None
    credential = None
    if client is None:
        load_dotenv(SRC_DIR / ".env")
        models = role_models_from_env(os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"])
        role_clients, credential = _foundry_clients(os.environ["FOUNDRY_PROJECT_ENDPOINT"], models)
        client = role_clients["coordinator"]
    directory = Path(data_dir or os.environ.get("LEARNING_COACH_DATA_DIR", SRC_DIR / ".learning-coach"))
    if not directory.is_absolute():
        directory = SRC_DIR / directory
    directory.mkdir(parents=True, exist_ok=True)
    native = os.environ.get("LEARNING_COACH_NATIVE_JSON", "0") == "1" if native_json is None else native_json
    runner = StructuredRunner(create_agents(client, INSTRUCTIONS_PATH, role_clients=role_clients), native_json=native)
    artifacts = ArtifactStore(directory / "artifacts", renderer=render_visual, public_base_url=public_base_url or
                              os.environ.get("LEARNING_COACH_PUBLIC_BASE_URL", "http://localhost:8088"))
    from learning_coach.adapters.illustrate import generate_image

    encoder()  # load the tokenizer once at startup, not inside the first turn

    return LearningPipeline(
        runner, ResearchService(runner, search_provider if search_provider is not None else default_provider()), _memory_store(directory, credential), artifacts,
        json.loads((SRC_DIR / "response.schema.json").read_text(encoding="utf-8")),
        image_generator=image_generator if image_generator is not None else generate_image,
        onboarding=os.environ.get("LEARNING_COACH_ONBOARDING", "1") == "1",
        demo_stage=demo_stage_from_env(),
        budgets=Budgets.from_env(),
    )



def create_server(pipeline=None):
    """Assemble an HTTP transport around the selected application service."""
    from learning_coach.api.http import create_server as build_http_server
    return build_http_server(pipeline if pipeline is not None else create_pipeline())
