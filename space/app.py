"""Hugging Face Space entrypoint: chat UI hosting the learning-coach pipeline.

The pipeline runs in-process; the Space's secret environment variables
(AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID /
FOUNDRY_PROJECT_ENDPOINT / AZURE_AI_MODEL_DEPLOYMENT_NAME) authenticate to
Azure AI Foundry. Visitors need no account and no credential.

Interactive visuals and generated illustrations render inline (iframe /
markdown image) from the artifact store, so they work on the public URL —
unlike the Foundry-hosted deployment whose gateway does not forward custom
artifact routes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

def _find_root() -> Path:
    """Local dev runs from space/ next to src/; deployed builds are flat."""
    here = Path(__file__).resolve().parent
    nested = here.parents[0] / "src/agent-framework-agent-with-local-tools-responses"
    if nested.is_dir():
        return nested
    return here


ROOT = _find_root()
sys.path.insert(0, str(ROOT))

import gradio as gr

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


from learning_coach.api.host import learner_text
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.server import create_pipeline

ARTIFACT_LINK = re.compile(r"\n?\[打开互动图\]\(http://localhost:8088/artifacts/([A-Za-z0-9_-]+)\)")
DATA_IMAGE = re.compile(r'data:image/[a-z]+;base64,[A-Za-z0-9+/=]+')


def build_pipeline() -> LearningPipeline:
    return create_pipeline(
        data_dir=Path(os.environ.get("LEARNING_COACH_DATA_DIR", "/tmp/learning-coach")).resolve(),
        public_base_url="http://localhost:8088", native_json=False,
    )


PIPELINE = build_pipeline()


def iframe_for(document: str) -> str:
    escaped = document.replace("&", "&amp;").replace('"', "&quot;")
    return (
        '<iframe srcdoc="' + escaped + '" '
        'style="width:100%;height:460px;border:1px solid #c7d8e3;border-radius:12px;'
        'background:#fff" sandbox="allow-scripts"></iframe>'
    )


def ensure_device(device):
    """Anonymous per-browser identity: a random ID plus a "visited before" flag.

    Kept in the visitor's own browser storage. It holds no personal data and
    lets learning records (when the learner turns them on) and the
    first-meeting introduction carry across conversations on this device.
    """
    if not isinstance(device, dict) or not isinstance(device.get("learner_id"), str) \
            or not device["learner_id"]:
        device = {"learner_id": uuid.uuid4().hex, "seen": False}
    return {"learner_id": device["learner_id"][:200], "seen": device.get("seen") is True}


async def respond(user_text, session, device=None):
    device = ensure_device(device)
    user_text = (user_text or "").strip()
    if not user_text:
        yield session, session["chat"], session["panel"], "", device
        return
    if session.get("locked"):
        yield session, session["chat"], session["panel"], "", device
        return
    session["locked"] = True
    try:
        # First token: acknowledge immediately so the UI never looks frozen.
        thinking = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "我先想想…"},
        ]
        yield session, thinking, session["panel"], "", device
        session["history"].append({"role": "user", "text": user_text})
        output, new_state = await asyncio.wait_for(
            PIPELINE.run(list(session["history"]), session_id=session["session_id"],
                         state=session["state"], learner_id=device["learner_id"],
                         returning_visitor=device["seen"]), timeout=240)
        device = {**device, "seen": True}
        session["state"] = new_state
        text = learner_text(output)
        # Keep model context separate from presentation-only markup and image bytes.
        history_text = ARTIFACT_LINK.sub("", text).rstrip()
        panel = session["panel"]
        match = ARTIFACT_LINK.search(text)
        if match:
            token = match.group(1)
            document = PIPELINE.artifacts.get(token)
            text = ARTIFACT_LINK.sub("", text).rstrip()
            if document:
                image = DATA_IMAGE.search(document)
                if image:
                    # Generated illustration: show the picture inline in chat.
                    text = f"{text}\n\n![配图]({image.group(0)})"
                else:
                    # Interactive visual: embedded sandboxed frame below chat.
                    panel = iframe_for(document)
        session["history"].append({"role": "assistant", "text": history_text})
        session["panel"] = panel
        session["chat"] = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": text},
        ]
        yield session, session["chat"], panel, "", device
    except Exception:
        session["chat"] = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "这次处理没有顺利完成，请再试一次。"},
        ]
        yield session, session["chat"], session["panel"], "", device
    finally:
        session["locked"] = False


def new_session():
    session = {"session_id": uuid.uuid4().hex, "state": None, "history": [],
               "chat": [], "panel": "", "locked": False}
    return session, [], "", ""


def verify_password(password):
    expected = os.environ.get("DEMO_PASSWORD")
    if expected and password != expected:
        return (
            gr.update(visible=True),
            gr.update(visible=False),
            "口令不对，请重试。",
        )
    return gr.update(visible=False), gr.update(visible=True), ""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="学习教练 · edu-agent") as demo:
        gr.Markdown(
            "# 学习教练 · edu-agent\n"
            "多 agent 学习教练：意图编排 · 检索查证 · 出题判分 · 学习记忆 · 互动图与配图。\n\n"
            "*Azure AI Foundry 驱动，无需登录即可体验。*"
        )
        if os.environ.get("DEMO_PASSWORD"):
            with gr.Group() as gate:
                pwd = gr.Textbox(label="演示口令", type="password")
                enter = gr.Button("进入")
                gate_msg = gr.Markdown("")
        with gr.Column(visible=not os.environ.get("DEMO_PASSWORD")) as main:
            chatbot = gr.Chatbot(label="对话", height=480)
            panel = gr.HTML("")
            with gr.Row():
                input_box = gr.Textbox(
                    placeholder="问点什么，比如：帮我生成一个对比 1/2 和 1/4 的互动图",
                    scale=9, label="")
                send = gr.Button("发送", scale=1, variant="primary")
            with gr.Row():
                reset = gr.Button("新会话")
                gr.Markdown("学习记录默认不保存；输入 /memory on 开启，/memory off 暂停，/forget 删除。"
                            "记录只和这台设备上的浏览器关联。")
        session = gr.State(new_session()[0])
        # Lives in this visitor's browser storage, so it survives "新会话" and reloads.
        device = gr.BrowserState({"learner_id": "", "seen": False}, storage_key="edu-agent-device")

        async def on_send(text, sess, dev):
            # respond is an async generator: first yields the "我先想想…"
            # placeholder, then the completed turn.
            async for result in respond(text, sess, dev):
                yield result

        outputs = [session, chatbot, panel, input_box, device]
        send.click(on_send, [input_box, session, device], outputs)
        input_box.submit(on_send, [input_box, session, device], outputs)
        reset.click(new_session, None, [session, chatbot, panel, input_box])
        if os.environ.get("DEMO_PASSWORD"):
            enter.click(verify_password, pwd, [gate, main, gate_msg])
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 8000)))
