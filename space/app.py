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

ROOT = Path(__file__).resolve().parents[1] / "src/agent-framework-agent-with-local-tools-responses"
sys.path.insert(0, str(ROOT))

import gradio as gr

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

from learning_coach.host import learner_text
from learning_coach.memory import LearningStore
from learning_coach.pipeline import LearningPipeline
from learning_coach.research import ResearchService
from learning_coach.runner import StructuredRunner, create_agents
from learning_coach.visuals import ArtifactStore

ARTIFACT_LINK = re.compile(r"\n?\[打开互动图\]\(http://localhost:8088/artifacts/([A-Za-z0-9_-]+)\)")
DATA_IMAGE = re.compile(r'data:image/[a-z]+;base64,[A-Za-z0-9+/=]+')


def build_pipeline() -> LearningPipeline:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )
    directory = Path(os.environ.get("LEARNING_COACH_DATA_DIR", "/tmp/learning-coach"))
    directory.mkdir(parents=True, exist_ok=True)
    runner = StructuredRunner(create_agents(client, ROOT / "instructions.md"), native_json=False)
    artifacts = ArtifactStore(directory / "artifacts", public_base_url="http://localhost:8088")
    return LearningPipeline(
        runner, ResearchService(runner), LearningStore(directory / "memory.sqlite3"), artifacts,
        json.loads((ROOT / "response.schema.json").read_text(encoding="utf-8")),
    )


PIPELINE = build_pipeline()


def iframe_for(document: str) -> str:
    escaped = document.replace("&", "&amp;").replace('"', "&quot;")
    return (
        '<iframe srcdoc="' + escaped + '" '
        'style="width:100%;height:460px;border:1px solid #c7d8e3;border-radius:12px;'
        'background:#fff" sandbox="allow-scripts"></iframe>'
    )


async def respond(user_text, session):
    user_text = (user_text or "").strip()
    if not user_text:
        return session, session["chat"], session["panel"], ""
    if session.get("locked"):
        return session, session["chat"], session["panel"], ""
    session["locked"] = True
    try:
        session["history"].append({"role": "user", "text": user_text})
        output, new_state = await asyncio.wait_for(
            PIPELINE.run(list(session["history"]), session_id=session["session_id"],
                         state=session["state"]), timeout=240)
        session["state"] = new_state
        text = learner_text(output)
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
        session["history"].append({"role": "assistant", "text": text})
        session["chat"] = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": text},
        ]
        return session, session["chat"], panel, ""
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
                gr.Markdown("记忆默认开启；输入 /memory off 暂停，/forget 删除记录。")
        session = gr.State(new_session()[0])

        async def on_send(text, sess):
            return await respond(text, sess)

        send.click(on_send, [input_box, session], [session, chatbot, panel, input_box])
        input_box.submit(on_send, [input_box, session], [session, chatbot, panel, input_box])
        reset.click(new_session, None, [session, chatbot, panel, input_box])
        if os.environ.get("DEMO_PASSWORD"):
            enter.click(verify_password, pwd, [gate, main, gate_msg])
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="0.0.0.0", server_port=7860)
