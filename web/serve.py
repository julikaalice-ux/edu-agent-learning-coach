"""Local dev proxy for the edu-agent chat page.

Serves web/index.html and forwards chat turns to the local Responses host
(`azd ai agent run` or `python main.py` on 127.0.0.1:8088) so the browser
page avoids CORS and never touches protocol details.

Run from the repository root (project env per docs/acceptance-v1.5.md):
  uv run --project src/agent-framework-agent-with-local-tools-responses \
      --locked --group dev python web/serve.py
Then open http://localhost:8600
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request

AGENT_URL = "http://127.0.0.1:8088/responses"
WEB_ROOT = Path(__file__).resolve().parent
PORT = 8600


def to_responses_input(history):
    items = []
    for turn in history:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            raise ValueError("invalid_history_role")
        text = turn.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("invalid_history_text")
        if turn["role"] == "user":
            items.append({"role": "user", "content": [{"type": "input_text", "text": text[:12000]}]})
        else:
            # Prior assistant turns use the plain-string content shape accepted
            # by the Responses create schema (output_text blocks are rejected).
            items.append({"type": "message", "role": "assistant", "content": text[:12000]})
    if not items or items[-1]["role"] != "user":
        raise ValueError("history_must_end_with_user")
    return items


def extract_output_text(payload):
    """Pull the single assistant output_text from a completed Responses object."""
    if not isinstance(payload, dict) or payload.get("status") not in ("completed", None):
        raise ValueError("response_not_completed")
    texts = []
    for item in payload.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    value = block.get("text")
                    if isinstance(value, str):
                        texts.append(value)
    if len(texts) != 1:
        raise ValueError("unexpected_output_shape")
    return texts[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "edu-agent-web/1.0"

    def log_message(self, *_args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (WEB_ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/health":
            try:
                with urllib.request.urlopen(AGENT_URL, timeout=3) as response:
                    return self._send(200, {"ok": True, "agent_status": response.status})
            except urllib.error.HTTPError:
                return self._send(200, {"ok": True})  # route exists (e.g. 405); agent is alive
            except Exception:
                return self._send(200, {"ok": False})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/chat":
            return self._send(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 200_000:
                return self._send(400, {"error": "bad_request"})
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = {"input": to_responses_input(request.get("history") or [])}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            agent_request = urllib.request.Request(
                AGENT_URL, data=body, method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"})
            with urllib.request.urlopen(agent_request, timeout=300) as response:
                text = extract_output_text(json.loads(response.read().decode("utf-8")))
            return self._send(200, {"text": text})
        except ValueError as error:
            return self._send(400, {"error": str(error)})
        except urllib.error.HTTPError as error:
            # The agent is alive but rejected the turn (e.g. validation); keep
            # protocol details server-side and let the user retry.
            return self._send(502, {"error": f"本地 agent 拒绝了该请求（HTTP {error.code}），请重试"})
        except urllib.error.URLError:
            return self._send(502, {"error": "本地 agent 未运行：请先在项目目录执行 azd ai agent run"})
        except TimeoutError:
            return self._send(504, {"error": "本地 agent 响应超时"})
        except Exception as error:  # keep protocol details server-side
            return self._send(502, {"error": f"代理错误：{type(error).__name__}"})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"edu-agent chat page: http://localhost:{PORT}  (agent: {AGENT_URL})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
