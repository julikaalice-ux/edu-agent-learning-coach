"""Hugging Face Space entrypoint: chat UI hosting the learning-coach pipeline.

The pipeline runs in-process; the Space's secret environment variables
(AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID /
FOUNDRY_PROJECT_ENDPOINT / AZURE_AI_MODEL_DEPLOYMENT_NAME) authenticate to
Azure AI Foundry. Visitors need no account and no credential.

When DEMO_ACCESS_KEY is set, only links carrying ?key=<that value> can chat;
the key is checked on page load and again in every turn. Daily caps
(DEMO_MAX_TURNS_PER_DAY, DEMO_MAX_IMAGES_PER_DAY; 0 = no cap) protect the
model budget behind the public URL.

Interactive visuals and generated illustrations render inline (iframe /
markdown image) from the artifact store, so they work on the public URL —
unlike the Foundry-hosted deployment whose gateway does not forward custom
artifact routes.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
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
CHINA_TIME = timezone(timedelta(hours=8))


def env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


class DailyLimit:
    """Count uses per calendar day (UTC+8); a limit of 0 means no cap.

    Kept in memory: a restart starts the count again, which is acceptable for
    a demo guard (the Azure budget alert is the hard backstop).
    """

    def __init__(self, limit: int):
        self.limit, self.day, self.used = limit, None, 0

    def take(self) -> bool:
        today = datetime.now(CHINA_TIME).date()
        if today != self.day:
            self.day, self.used = today, 0
        if self.limit and self.used >= self.limit:
            return False
        self.used += 1
        return True


ACCESS_KEY = os.environ.get("DEMO_ACCESS_KEY", "").strip()
TURN_LIMIT = DailyLimit(env_int("DEMO_MAX_TURNS_PER_DAY", 150))
IMAGE_LIMIT = DailyLimit(env_int("DEMO_MAX_IMAGES_PER_DAY", 20))
LOCKED_TEXT = "这是私人演示，请用收到的邀请链接打开。"
LIMIT_TEXT = "今天的演示次数用完了，明天再来试试吧。"


def key_matches(given) -> bool:
    if not ACCESS_KEY:
        return True
    return isinstance(given, str) and hmac.compare_digest(given.encode(), ACCESS_KEY.encode())


async def limited_image(prompt: str, **options) -> bytes:
    """Image generation costs the most per call, so it has its own daily cap."""
    if not IMAGE_LIMIT.take():
        # RuntimeError is reported to the learner as "image service unavailable".
        raise RuntimeError("image_daily_limit")
    from learning_coach.adapters.illustrate import generate_image

    return await generate_image(prompt, **options)


def build_pipeline() -> LearningPipeline:
    return create_pipeline(
        data_dir=Path(os.environ.get("LEARNING_COACH_DATA_DIR", "/tmp/learning-coach")).resolve(),
        public_base_url="http://localhost:8088", native_json=False,
        image_generator=limited_image,
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


async def respond(user_text, session, device=None, allowed=False):
    device = ensure_device(device)
    user_text = (user_text or "").strip()
    if not user_text:
        yield session, session["chat"], session["panel"], "", device
        return
    if session.get("locked"):
        yield session, session["chat"], session["panel"], "", device
        return
    # Checked here, not only by hiding the page, so direct API calls are refused too.
    if not allowed or not TURN_LIMIT.take():
        session["chat"] = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": LIMIT_TEXT if allowed else LOCKED_TEXT},
        ]
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
        session["options"] = options_from(output)
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


OPTION_LINE = re.compile(r"^\s*\d+\.\s*(.+?)\s*$", re.MULTILINE)
MAX_OPTIONS = 8
DEMO_STAGE = os.environ.get("LEARNING_COACH_DEMO_STAGE", "高中") \
    if os.environ.get("LEARNING_COACH_DEMO_MODE", "0") == "1" else None


def options_from(output) -> list[str]:
    """Numbered choices in the agent's question become clickable buttons."""
    step = (output or {}).get("next_step") or {}
    if step.get("kind") != "question":
        return []
    found = OPTION_LINE.findall(step.get("text", ""))
    if not found:
        return []
    if "跳过" in step.get("text", "") and "跳过" not in found:
        found.append("跳过")
    return found[:MAX_OPTIONS]


def option_updates(session):
    labels = session.get("options", [])
    return [gr.update(value=labels[i], visible=True) if i < len(labels) else gr.update(visible=False)
            for i in range(MAX_OPTIONS)]


def clear_demo(session, device):
    """Start the demo from a clean slate: drop this browser's records and identity."""
    device = ensure_device(device)
    for remove, key in ((PIPELINE.store.clear, device["learner_id"]),
                        (PIPELINE.artifacts.delete_session, session.get("session_id", ""))):
        try:
            remove(key)
        except Exception:
            pass
    fresh = new_session()
    return (*fresh, {"learner_id": uuid.uuid4().hex, "seen": False})


def new_session():
    session = {"session_id": uuid.uuid4().hex, "state": None, "history": [],
               "chat": [], "panel": "", "locked": False}
    return session, [], "", ""


def check_access(request: gr.Request):
    """On page load: the invite key travels in the link as ?key=..."""
    try:
        given = request.query_params.get("key")
    except AttributeError:
        given = None
    allowed = key_matches(given)
    return allowed, gr.update(visible=allowed), gr.update(visible=not allowed)


def build_app() -> gr.Blocks:
    with gr.Blocks(title="学习教练 · edu-agent") as demo:
        gr.Markdown(
            "# 学习教练 · edu-agent\n"
            "多 agent 学习教练：意图编排 · 检索查证 · 出题判分 · 学习记忆 · 互动图与配图。\n\n"
            "*Azure AI Foundry 驱动，无需登录即可体验。*"
        )
        locked_note = gr.Markdown(f"**{LOCKED_TEXT}**", visible=False)
        with gr.Column(visible=not ACCESS_KEY) as main:
            chatbot = gr.Chatbot(label="对话", height=480)
            panel = gr.HTML("")
            with gr.Row():
                option_buttons = [gr.Button(visible=False, size="sm") for _ in range(MAX_OPTIONS)]
            with gr.Row():
                input_box = gr.Textbox(
                    placeholder="问点什么，比如：帮我生成一个对比 1/2 和 1/4 的互动图",
                    scale=9, label="")
                send = gr.Button("发送", scale=1, variant="primary")
            with gr.Row():
                reset = gr.Button("新会话")
                if DEMO_STAGE:
                    clear = gr.Button("清空演示数据")
                    gr.Markdown(f"**演示模式**：跳过了年龄和学习记录的询问，预设学段“{DEMO_STAGE}”，学习记录已开启。")
                else:
                    gr.Markdown("第一次使用时会问学段和是否保存学习记录；之后随时可以说“开启记录”“关闭记录”“删除记录”。"
                                "记录只和这台设备上的浏览器关联。")
        session = gr.State(new_session()[0])
        # Lives in this visitor's browser storage, so it survives "新会话" and reloads.
        device = gr.BrowserState({"learner_id": "", "seen": False}, storage_key="edu-agent-device")
        # Server-side per-visitor flag; set from the link on page load.
        allowed = gr.State(not ACCESS_KEY)

        async def on_send(text, sess, dev, ok):
            # respond is an async generator: first yields the "我先想想…"
            # placeholder, then the completed turn.
            async for result in respond(text, sess, dev, ok):
                yield result

        outputs = [session, chatbot, panel, input_box, device]
        send.click(on_send, [input_box, session, device, allowed], outputs).then(
            option_updates, [session], option_buttons)
        input_box.submit(on_send, [input_box, session, device, allowed], outputs).then(
            option_updates, [session], option_buttons)
        for button in option_buttons:
            # A button passes its own label, so clicking it sends the chosen option.
            button.click(on_send, [button, session, device, allowed], outputs).then(
                option_updates, [session], option_buttons)
        reset.click(new_session, None, [session, chatbot, panel, input_box]).then(
            option_updates, [session], option_buttons)
        if DEMO_STAGE:
            clear.click(clear_demo, [session, device], [session, chatbot, panel, input_box, device]).then(
                option_updates, [session], option_buttons)
        demo.load(check_access, None, [allowed, main, locked_note])
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 8000)))
