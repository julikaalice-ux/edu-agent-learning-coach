"""Responses transport and artifact routes; receives an application service."""
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.responses import InMemoryResponseProvider
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from .host import LearningCoachAgent


def create_server(pipeline):
    active = pipeline

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

