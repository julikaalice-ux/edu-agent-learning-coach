"""edu-agent 演示前端的无状态代理：持有 Azure 凭据，转发到托管 agent。

前端只与本代理对话，凭据不出现在浏览器。令牌通过 Azure CLI 的 scope
形式获取（租户的 resource 形式有 AADSTS500011 问题），服务主体环境变量
存在时优先走服务主体。

用法：
    PORT=8787 python backend/proxy.py     # 默认转发托管 agent 的 Responses 端点
    AGENT_ENDPOINT=... python backend/proxy.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCOPE = "https://ai.azure.com/.default"
DEFAULT_ENDPOINT = ("https://cog-c5e2qyhj2tb7g.services.ai.azure.com/api/projects/edu-agent"
                    "/agents/agent-framework-agent-with-local-tools-responses/endpoint"
                    "/protocols/openai")
ENDPOINT = os.environ.get("AGENT_ENDPOINT", DEFAULT_ENDPOINT)
# The hosted agent publishes its visuals here (anonymous read, cannot be listed;
# the same container as LEARNING_COACH_ARTIFACT_BLOB_URL in azure.yaml).
DEFAULT_ARTIFACT_BASE = "https://eduartifacts5044621b.blob.core.windows.net/artifacts"
ARTIFACT_BASE = os.environ.get("ARTIFACT_BLOB_URL", DEFAULT_ARTIFACT_BASE).rstrip("/")
ARTIFACT_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024   # a picture page with several photos stays well below this
# What a visual page may do inside the demo page; the Space sends the same.
# No X-Frame-Options: the demo page itself is often shown in a frame of
# another site (huggingface.co, modelscope.cn), and a same-origin rule then
# blocks the visual. The page holds public teaching material only.
ARTIFACT_POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                   "img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'; sandbox allow-scripts")
ARTIFACT_ROUTE = re.compile(r"/artifact/([A-Za-z0-9_-]+)(/image)?")
DATA_IMAGE = re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+")
TOKEN_LOCK = threading.Lock()
_TOKEN = {"value": None, "expires": 0}

OPTION_LINE = re.compile(r"^\s*([A-D]|\d+)\.\s*(.+?)\s*$", re.MULTILINE)
QUESTION_LINES = re.compile(r"(A|B|C|D)\.\s", re.MULTILINE)


def get_token() -> str:
    """令牌来源：服务主体环境变量（Space/部署环境）优先，其次 Azure CLI scope 形式（本地）。

    按到期前 5 分钟缓存。
    """
    import time

    with TOKEN_LOCK:
        if _TOKEN["value"] and _TOKEN["expires"] - time.time() > 300:
            return _TOKEN["value"]
        tenant = os.environ.get("AZURE_TENANT_ID", "").strip()
        client = os.environ.get("AZURE_CLIENT_ID", "").strip()
        secret = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
        if tenant and client and secret:
            data = urllib.parse.urlencode({
                "client_id": client, "client_secret": secret,
                "grant_type": "client_credentials", "scope": SCOPE,
            }).encode()
            request = urllib.request.Request(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data=data, method="POST")
            failure = "service_principal_token_failed"
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read())
                # The OAuth token endpoint answers in snake_case (access_token,
                # expires_in); only the Azure CLI uses accessToken / expires_on.
                _TOKEN["value"] = payload["access_token"]
                _TOKEN["expires"] = int(time.time()) + int(payload.get("expires_in", 3600))
                return _TOKEN["value"]
            except urllib.error.HTTPError as error:
                failure = f"service_principal_token_http_{error.code}"
            except (urllib.error.URLError, KeyError, ValueError):
                pass
        else:
            failure = "service_principal_not_configured"
        # Fall back to the Azure CLI (local use). In a container without it,
        # report why the service principal did not work instead of a bare
        # FileNotFoundError for the missing az command.
        if shutil.which("az") is None:
            raise RuntimeError(failure)
        result = subprocess.run(
            ["az", "account", "get-access-token", "--scope", SCOPE, "--output", "json"],
            capture_output=True, text=True, check=False, timeout=60)
        if result.returncode != 0:
            raise RuntimeError("azure_token_unavailable")
        payload = json.loads(result.stdout)
        _TOKEN["value"] = payload["accessToken"]
        _TOKEN["expires"] = int(payload["expires_on"])
        return _TOKEN["value"]


def fetch_artifact(token: str) -> str | None:
    """A visual the hosted agent published, as its HTML page; None when there is no such page."""
    if not ARTIFACT_TOKEN.fullmatch(token or ""):
        return None
    try:
        with urllib.request.urlopen(f"{ARTIFACT_BASE}/{token}.html", timeout=20) as response:
            data = response.read(MAX_ARTIFACT_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"artifact_http_{error.code}") from None
    if len(data) > MAX_ARTIFACT_BYTES:
        raise RuntimeError("artifact_too_large")
    return data.decode("utf-8")


def artifact_reply(path: str):
    """(status, content type, body) for /artifact/<token> and /artifact/<token>/image; None for other paths.

    Local runs (vite + this proxy) need the same route as the Space: the
    page shows a visual in a frame from /artifact/<token>, and a question
    picture from /artifact/<token>/image.
    """
    match = ARTIFACT_ROUTE.fullmatch(path)
    if match is None:
        return None
    try:
        document = fetch_artifact(match.group(1))
    except Exception as error:
        code = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        return 502, "application/json", json.dumps({"error": code}).encode()
    if document is None:
        return 404, "application/json", b'{"error": "not_found"}'
    if not match.group(2):
        return 200, "text/html; charset=utf-8", document.encode()
    found = DATA_IMAGE.search(document)
    if found is None:
        return 404, "application/json", b'{"error": "not_found"}'
    media_type, data = found.group(0)[len("data:"):].split(";base64,", 1)
    return 200, media_type, base64.b64decode(data)


def call(path: str, body: dict | None = None, method: str | None = None) -> dict:
    url = f"{ENDPOINT}/{path}?api-version=v1"
    data = json.dumps(body or {}).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Authorization": f"Bearer {get_token()}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"agent_http_{error.code}") from error


def stream_events(conversation: str, user_input: str):
    """把上游 SSE 转成简化事件：逐段 {"delta"}，最后 {"done"}。

    上游 events: response.output_text.delta（增量文本）与 response.completed
    （完整输出）；代理只透传可见文本，其余事件丢弃。
    """
    url = f"{ENDPOINT}/responses?api-version=v1"
    body = json.dumps({"conversation": conversation, "input": user_input, "stream": True}).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {get_token()}",
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        event, data_lines = None, []
        for raw in response:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if event and data_lines:
                    yield event, "\n".join(data_lines)
                event, data_lines = None, []
                continue
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())


def stream_reply(conversation: str, user_input: str):
    """逐段产出回复文本，最后产出 (conversation, 全文)。"""
    full = []
    for event, data in stream_events(conversation, user_input):
        if event == "response.output_text.delta":
            try:
                delta = json.loads(data).get("delta", "")
            except json.JSONDecodeError:
                continue
            if delta:
                full.append(delta)
                yield {"delta": delta}
        elif event == "response.completed":
            try:
                payload = json.loads(data)
                text = extract_text(payload.get("response") or payload)
                if text:
                    full = [text]
            except json.JSONDecodeError:
                pass
    yield {"done": True, "conversation": conversation,
           "text": "".join(full).strip() or "这次处理没有顺利完成，请再试一次。"}


def extract_text(payload: dict) -> str:
    text = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                text.append(content["text"])
    return "\n\n".join(text).strip()


def extract_options(text: str) -> list[dict]:
    """教练回复里的选项变成前端卡片：{"key": 字母/编号, "text": 选项文字}。"""
    if not QUESTION_LINES.search(text):
        return []
    found = [{"key": key, "text": value} for key, value in OPTION_LINE.findall(text)]
    if "跳过" in text and not any(item["text"] == "跳过" for item in found):
        found.append({"key": "", "text": "跳过"})
    return found[:8]


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True})
            return
        artifact = artifact_reply(urllib.parse.urlsplit(self.path).path)
        if artifact is None:
            self._reply(404, {"error": "not_found"})
            return
        status, content_type, body = artifact
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", ARTIFACT_POLICY)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/turn":
            self._reply(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            user_input = str(payload.get("input", "")).strip()
            if not user_input:
                self._reply(400, {"error": "empty_input"})
                return
            conversation = payload.get("conversation")
            if not conversation:
                created = call("conversations", {})
                conversation = created.get("id")
                if not conversation:
                    self._reply(502, {"error": "conversation_create_failed"})
                    return
            if payload.get("stream"):
                # 简化 SSE：{delta} 逐段，最后 {done} 带全文与选项。
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for event in stream_reply(conversation, user_input):
                    if "delta" in event:
                        frame = json.dumps({"delta": event["delta"]}, ensure_ascii=False)
                    else:
                        frame = json.dumps({"done": True, "conversation": event["conversation"],
                                            "text": event["text"],
                                            "options": extract_options(event["text"])},
                                           ensure_ascii=False)
                    self.wfile.write(f"data: {frame}\n\n".encode())
                    self.wfile.flush()
                return
            result = call("responses", {"conversation": conversation, "input": user_input})
            text = extract_text(result) or "这次处理没有顺利完成，请再试一次。"
            self._reply(200, {"conversation": conversation, "text": text,
                              "options": extract_options(text)})
        except Exception as error:
            self._reply(502, {"error": type(error).__name__})

    def log_message(self, *args):
        pass  # 代理不记录请求内容


def main():
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"edu-agent proxy on http://127.0.0.1:{port} -> {ENDPOINT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
