"""Shared composition root for hosted API and Gradio entry points.

Only this module chooses the concrete model, storage and search adapters.
Importing it does not create clients, databases, or agents.
"""

import json
import logging
import os
from pathlib import Path
from time import perf_counter

LOG = logging.getLogger(__name__)

from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from learning_coach.db.memory import LearningStore
from learning_coach.domain.context import Budgets, encoder
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.research import ResearchService
from learning_coach.adapters.search_providers import default_provider
from learning_coach.adapters.image_search import default_image_finder
from learning_coach.adapters.runner import ROLES, StructuredRunner, create_agents
from learning_coach.adapters.single_runner import MAX_ITERATIONS as SINGLE_MAX_ITERATIONS
from learning_coach.adapters.single_runner import SingleAgentRunner, create_single_agent
from learning_coach.service.single_agent import SingleAgentPipeline
from learning_coach.adapters.visuals import render_visual
from learning_coach.db.artifacts import ArtifactStore

SRC_DIR = Path(__file__).resolve().parent.parent
INSTRUCTIONS_PATH = SRC_DIR / "prompts/instructions.md"
PROMPTS_DIR = INSTRUCTIONS_PATH.parent


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
    # The scope-form CLI credential goes first: some tenants intermittently
    # reject the resource form (AADSTS500011) while the scope form works.
    from learning_coach.adapters.scope_credential import chained_credential
    credential = chained_credential()
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


def _artifact_store(directory: Path, public_base_url: str | None, credential):
    """File ArtifactStore, or a public blob wrapper for the hosted entry.

    LEARNING_COACH_ARTIFACT_BLOB_URL unset = local /artifacts route (Space and
    local demo). When set, prepared artifact URLs point at the public blob
    container; upload failures degrade to the local file (the link 404s) and
    are logged, never raised. A malformed URL degrades to the plain store.
    """
    inner = ArtifactStore(directory / "artifacts", renderer=render_visual, public_base_url=public_base_url or
                          os.environ.get("LEARNING_COACH_PUBLIC_BASE_URL", "http://localhost:8088"))
    blob_url = os.environ.get("LEARNING_COACH_ARTIFACT_BLOB_URL", "").strip()
    if not blob_url:
        return inner
    from learning_coach.adapters.blob_artifacts import BlobArtifactStore
    try:
        return BlobArtifactStore(inner, blob_url, credential=credential)
    except ValueError as error:
        LOG.warning("artifact_blob_wiring_disabled error=%s", error)
        return inner


def _timed_pictures(generate, finder, observer):
    """Report picture generation and web picture search to the stage observer, like model calls.

    Records carry only role, phase, outcome, counts and elapsed time.
    """
    if observer is None:
        return generate, finder

    def report(role, phase, outcome, started, **extra):
        observer({"role": role, "phase": phase, "attempt": 1, "outcome": outcome,
                  "elapsed_ms": round((perf_counter() - started) * 1000), **extra})

    async def timed_generate(prompt, **options):
        started, outcome = perf_counter(), "failed"
        try:
            image = await generate(prompt, **options)
            outcome = "ok"
            return image
        finally:
            report("image", "generate", outcome, started)

    class TimedFinder:
        async def find(self, query_zh, query_en, *, limit=3):
            started, found = perf_counter(), None
            try:
                found = await finder.find(query_zh, query_en, limit=limit)
                return found
            finally:
                report("image_search", "search", "ok" if found is not None else "failed", started,
                       results=len(found or []))

    return (timed_generate if generate is not None else None,
            TimedFinder() if finder is not None else None)


ARCHITECTURES = ("multi", "single")


def architecture_from_env() -> str:
    """LEARNING_COACH_ARCHITECTURE=single runs the single-agent baseline (comparison only)."""
    value = os.environ.get("LEARNING_COACH_ARCHITECTURE", "multi").strip() or "multi"
    if value not in ARCHITECTURES:
        raise ValueError("unknown_architecture")
    return value


def create_pipeline(*, client=None, data_dir=None, public_base_url=None, native_json=None,
                    image_generator=None, search_provider=None, stage_observer=None, architecture=None,
                    image_finder=None):
    """Build dependencies without launching a server or contacting a model.

    architecture: "multi" (default, the product) or "single" (the single-agent
    baseline used for comparison). Both share storage, search, rendering,
    image generation and every program check.
    """
    role_clients = None
    credential = None
    if client is None:
        load_dotenv(SRC_DIR / ".env")
    architecture = architecture or architecture_from_env()
    if architecture not in ARCHITECTURES:
        raise ValueError("unknown_architecture")
    if client is None and architecture == "multi":
        models = role_models_from_env(os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"])
        role_clients, credential = _foundry_clients(os.environ["FOUNDRY_PROJECT_ENDPOINT"], models)
        client = role_clients["coordinator"]
    elif client is None:
        from learning_coach.adapters.scope_credential import chained_credential
        credential = chained_credential()
        model = (os.environ.get("LEARNING_COACH_MODEL_SINGLE", "").strip()
                 or os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"])
        client = FoundryChatClient(project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], model=model,
                                   credential=credential,
                                   function_invocation_configuration={"max_iterations": SINGLE_MAX_ITERATIONS})
    directory = Path(data_dir or os.environ.get("LEARNING_COACH_DATA_DIR", SRC_DIR / ".learning-coach"))
    if not directory.is_absolute():
        directory = SRC_DIR / directory
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = _artifact_store(directory, public_base_url, credential)
    from learning_coach.adapters.illustrate import generate_image, use_credential
    if credential is not None:
        use_credential(credential)

    encoder()  # load the tokenizer once at startup, not inside the first turn

    provider = search_provider if search_provider is not None else default_provider()
    pictures, finder = _timed_pictures(image_generator if image_generator is not None else generate_image,
                                       image_finder if image_finder is not None else default_image_finder(),
                                       stage_observer)
    shared = dict(
        image_generator=pictures,
        image_finder=finder,
        onboarding=os.environ.get("LEARNING_COACH_ONBOARDING", "1") == "1",
        demo_stage=demo_stage_from_env(),
        budgets=Budgets.from_env(),
    )
    response_schema = json.loads((SRC_DIR / "response.schema.json").read_text(encoding="utf-8"))
    store = _memory_store(directory, credential)
    if architecture == "single":
        agent = SingleAgentRunner(create_single_agent(client, PROMPTS_DIR), observer=stage_observer)
        return SingleAgentPipeline(agent, provider, store, artifacts, response_schema, **shared)
    native = os.environ.get("LEARNING_COACH_NATIVE_JSON", "0") == "1" if native_json is None else native_json
    runner = StructuredRunner(create_agents(client, INSTRUCTIONS_PATH, role_clients=role_clients),
                              native_json=native, observer=stage_observer)
    return LearningPipeline(runner, ResearchService(runner, provider), store, artifacts, response_schema, **shared)



def create_server(pipeline=None):
    """Assemble an HTTP transport around the selected application service."""
    from learning_coach.api.http import create_server as build_http_server
    return build_http_server(pipeline if pipeline is not None else create_pipeline())
