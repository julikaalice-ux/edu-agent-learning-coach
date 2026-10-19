"""edu-agent 演示前端的无状态代理：持有 Azure 凭据，转发到托管 agent。

前端只与本代理对话，凭据不出现在浏览器。令牌通过 Azure CLI 的 scope
形式获取（租户的 resource 形式有 AADSTS500011 问题），服务主体环境变量
存在时优先走服务主体。

用法：
    PORT=8787 python backend/proxy.py     # 默认转发托管 agent 的 Responses 端点
    AGENT_ENDPOINT=... python backend/proxy.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCOPE = "https://ai.azure.com/.default"
DEFAULT_ENDPOINT = ("https://cog-c5e2qyhj2tb7g.services.ai.azure.com/api/projects/edu-agent"
                    "/agents/agent-framework-agent-with-local-tools-responses/endpoint"
                    "/protocols/openai")
ENDPOINT = os.environ.get("AGENT_ENDPOINT", DEFAULT_ENDPOINT)
TOKEN_LOCK = threading.Lock()
_TOKEN = {"value": None, "expires": 0}

OPTION_LINE = re.compile(r"^\s*(?:[A-D]|\d+)\.\s*(.+?)\s*$", re.MULTILINE)
QUESTION_LINES = re.compile(r"(A|B|C|D)\.\s", re.MULTILINE)


def get_token() -> str:
    """Azure CLI scope 形式的令牌，按到期前 5 分钟缓存。"""
    import time

    with TOKEN_LOCK:
        if _TOKEN["value"] and _TOKEN["expires"] - time.time() > 300:
            return _TOKEN["value"]
        result = subprocess.run(
            ["az", "account", "get-access-token", "--scope", SCOPE, "--output", "json"],
            capture_output=True, text=True, check=False, timeout=60)
        if result.returncode != 0:
            raise RuntimeError("azure_token_unavailable")
        payload = json.loads(result.stdout)
        _TOKEN["value"] = payload["accessToken"]
        _TOKEN["expires"] = int(payload["expires_on"])
        return _TOKEN["value"]


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


def extract_options(text: str) -> list[str]:
    """教练 next_step 里的编号选项变成前端胶囊按钮。"""
    if not QUESTION_LINES.search(text):
        return []
    found = OPTION_LINE.findall(text)
    if "跳过" in text and "跳过" not in found:
        found.append("跳过")
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
        self._reply(404, {"error": "not_found"})

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
