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
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.research import ResearchService
from learning_coach.adapters.search_providers import default_provider
from learning_coach.adapters.runner import StructuredRunner, create_agents
from learning_coach.adapters.visuals import render_visual
from learning_coach.db.artifacts import ArtifactStore

SRC_DIR = Path(__file__).resolve().parent.parent
INSTRUCTIONS_PATH = SRC_DIR / "instructions.md"


def create_pipeline(*, client=None, data_dir=None, public_base_url=None, native_json=None,
                    image_generator=None, search_provider=None):
    """Build dependencies without launching a server or contacting a model."""
    if client is None:
        load_dotenv(SRC_DIR / ".env")
        client = FoundryChatClient(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
            credential=DefaultAzureCredential(),
        )
    directory = Path(data_dir or os.environ.get("LEARNING_COACH_DATA_DIR", SRC_DIR / ".learning-coach"))
    if not directory.is_absolute():
        directory = SRC_DIR / directory
    directory.mkdir(parents=True, exist_ok=True)
    native = os.environ.get("LEARNING_COACH_NATIVE_JSON", "0") == "1" if native_json is None else native_json
    runner = StructuredRunner(create_agents(client, INSTRUCTIONS_PATH), native_json=native)
    artifacts = ArtifactStore(directory / "artifacts", renderer=render_visual, public_base_url=public_base_url or
                              os.environ.get("LEARNING_COACH_PUBLIC_BASE_URL", "http://localhost:8088"))
    from learning_coach.adapters.illustrate import generate_image

    return LearningPipeline(
        runner, ResearchService(runner, search_provider if search_provider is not None else default_provider()), LearningStore(directory / "memory.sqlite3"), artifacts,
        json.loads((SRC_DIR / "response.schema.json").read_text(encoding="utf-8")),
        image_generator=image_generator if image_generator is not None else generate_image,
    )



def create_server(pipeline=None):
    """Assemble an HTTP transport around the selected application service."""
    from learning_coach.api.http import create_server as build_http_server
    return build_http_server(pipeline if pipeline is not None else create_pipeline())
