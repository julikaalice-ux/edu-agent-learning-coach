# Copyright (c) Microsoft. All rights reserved.
"""Five in-process agents behind one validated Responses endpoint."""

import json
import logging
import os
from pathlib import Path

from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.responses import InMemoryResponseProvider
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from learning_coach.host import LearningCoachAgent
from learning_coach.memory import LearningStore
from learning_coach.pipeline import LearningPipeline
from learning_coach.research import ResearchService
from learning_coach.runner import StructuredRunner, create_agents
from learning_coach.visuals import ArtifactStore


SRC_DIR = Path(__file__).resolve().parent
INSTRUCTIONS_PATH = SRC_DIR / "instructions.md"


def create_pipeline(*, client=None, data_dir=None, public_base_url=None, native_json=None):
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
    artifacts = ArtifactStore(directory / "artifacts", public_base_url=public_base_url or
                              os.environ.get("LEARNING_COACH_PUBLIC_BASE_URL", "http://localhost:8088"))
    return LearningPipeline(
        runner, ResearchService(runner), LearningStore(directory / "memory.sqlite3"), artifacts,
        json.loads((SRC_DIR / "response.schema.json").read_text(encoding="utf-8")),
    )


def create_server(pipeline=None):
    active = pipeline if pipeline is not None else create_pipeline()

    async def artifact(request):
        html = active.artifacts.get(request.path_params["token"])
        if html is None:
            return Response(status_code=404)
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                       "img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'; sandbox allow-scripts",
        })

    # Keep protocol transcripts in process memory; the SDK's default file store
    # otherwise persists raw conversations independently of learning-memory opt-in.
    return ResponsesHostServer(LearningCoachAgent(active), store=InMemoryResponseProvider(),
                               configure_observability=None,
                               routes=[Route("/artifacts/{token}", artifact, methods=["GET"])])


def main():
    logger = logging.getLogger("learning_coach")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    create_server().run()


if __name__ == "__main__":
    main()
