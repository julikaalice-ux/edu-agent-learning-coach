#!/usr/bin/env python3
"""Dev proxy: lets a local browser frontend talk to the deployed Foundry agent.

The proxy is the only component holding the Azure AD credential; it injects a
fresh token per request, so the frontend stays a pure static page that only
calls http://localhost:8787. For local demos and development only — do not
expose this port beyond localhost.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "agent-framework-agent-with-local-tools-responses"
SCOPE = "https://ai.azure.com/.default"
PORT = int(os.environ.get("LEARNING_COACH_PROXY_PORT", "8787"))

SRC = Path(__file__).resolve().parents[1] / "src/agent-framework-agent-with-local-tools-responses"


def upstream_base() -> tuple[str, bool]:
    """Return (upstream base url, requires_auth).

    Mode cloud (default): forward to the deployed Foundry agent; each request
    gets a fresh Azure AD token injected by the proxy.
    Mode local (LEARNING_COACH_PROXY_TARGET=local): forward to a locally
    running `python main.py` agentserver so interactive-artifact links
    (http://localhost:8088/artifacts/...) resolve in the demo browser.
    """
    if os.environ.get("LEARNING_COACH_PROXY_TARGET", "cloud") == "local":
        base = os.environ.get("LEARNING_COACH_LOCAL_UPSTREAM", "http://localhost:8088")
        return base.rstrip("/"), False
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    env_file = SRC / ".env"
    if not endpoint and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("FOUNDRY_PROJECT_ENDPOINT="):
                endpoint = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not endpoint:
        raise SystemExit("FOUNDRY_PROJECT_ENDPOINT not found in environment or src .env")
    return endpoint.rstrip("/") + f"/agents/{APP}/endpoint/protocols/openai", True


UPSTREAM, REQUIRES_AUTH = upstream_base()

_credential = None


def token() -> str:
    global _credential
    if _credential is None:
        from azure.identity import DefaultAzureCredential
        _credential = DefaultAzureCredential()
    return _credential.get_token(SCOPE).token


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass  # keep the demo terminal quiet

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization,content-type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._reply(204, b"")

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            self._reply(200, b'{"ok":true}')
        else:
            self._reply(404, b'{"error":"not_found"}')

    def do_POST(self):
        if self.path not in ("/conversations", "/responses"):
            self._reply(404, b'{"error":"not_found"}')
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        payload = self.rfile.read(length)
        url = f"{UPSTREAM}{self.path}?api-version=v1"
        headers = {"Content-Type": "application/json"}
        if REQUIRES_AUTH:
            headers["Authorization"] = f"Bearer {token()}"
        request = urllib.request.Request(url, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=240) as upstream:
                self._reply(upstream.status, upstream.read())
        except urllib.error.HTTPError as error:
            # Pass upstream status and body through so the frontend can react.
            self._reply(error.code, error.read())
        except (urllib.error.URLError, TimeoutError, OSError):
            self._reply(502, b'{"error":{"message":"upstream_unreachable"}}')


if __name__ == "__main__":
    print(f"proxy ({'local agent' if not REQUIRES_AUTH else 'cloud endpoint'}) → {UPSTREAM}")
    print(f"listening on http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Proxy).serve_forever()
