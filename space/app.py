"""Hugging Face Space entrypoint: chat UI hosting the learning-coach pipeline.

The pipeline runs in-process; the Space's secret environment variables
(AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID /
FOUNDRY_PROJECT_ENDPOINT / AZURE_AI_MODEL_DEPLOYMENT_NAME) authenticate to
Azure AI Foundry. Visitors need no account and no credential.

When DEMO_ACCESS_KEY is set, only links carrying ?key=<that value> can chat;
the key is checked on page load and again in every turn. Daily caps
(DEMO_MAX_TURNS_PER_DAY, DEMO_MAX_IMAGES_PER_DAY; 0 = no cap) protect the
model budget behind the public URL.

Every page open is a brand-new session: the chat, the learning records and
the coach's memory all belong to that open page only, and closing it leaves
nothing the next open can read. The only thing kept in the visitor's own
browser is a bare "visited before" flag (no learning content) that skips the
first-meeting questions; it is encrypted with a stable key
(LEARNING_COACH_BROWSER_SECRET, or one saved in the data folder) so it
survives restarts. "我的笔记" shows the notes compiled in the current
session.

Interactive visuals and generated illustrations render inline (iframe /
markdown image) from the artifact store, so they work on the public URL —
unlike the Foundry-hosted deployment whose gateway does not forward custom
artifact routes.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Mirror agent/main.py: the package logs at INFO, and the default root
# logger (lastResort) would otherwise swallow those lines on the Space.
logger = logging.getLogger("learning_coach")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())

def _find_root() -> Path:
    """Local dev runs from space/ next to agent/; deployed builds are flat."""
    here = Path(__file__).resolve().parent
    nested = here.parents[0] / "agent"
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
from learning_coach.domain.mastery import LEARNING, SOLID

# Interactive visuals and question pictures both arrive as artifact links.
ARTIFACT_LINK = re.compile(r"\n?\[(?:打开互动图|看题目配图)\]\(http://localhost:8088/artifacts/([A-Za-z0-9_-]+)\)")
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


DATA_DIR = Path(os.environ.get("LEARNING_COACH_DATA_DIR", "/tmp/learning-coach")).resolve()


def browser_secret(data_dir: Path = DATA_DIR):
    """A stable key for browser storage, so the "visited before" flag survives restarts.

    Without it Gradio picks a new random key on every start and the flag can
    no longer be read back; visitors then get the first-meeting questions
    again, which is harmless.
    """
    configured = os.environ.get("LEARNING_COACH_BROWSER_SECRET", "").strip()
    if configured:
        return configured
    path = data_dir / "browser_secret"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(24)
        path.write_text(value, encoding="utf-8")
        return value
    except OSError:
        return None  # Gradio then uses a random key for this run only


# Progress hints shown in the chat while a turn runs: the runner's per-stage
# observer updates the current label, and the chat generator polls it while
# awaiting the pipeline so long stages (visuals) never look frozen.
STAGE_LABELS = {
    ("coordinator", "decide"): "正在理解你的问题…",
    ("verifier", "specialist"): "正在查证资料…",
    ("quizzer", "quiz"): "正在出题…",
    ("notes", "specialist"): "正在整理学习笔记…",
    ("visualizer", "specialist"): "正在生成互动图，可能需要半分钟左右…",
    ("verifier", "visual_review"): "正在审阅图示…",
    ("coordinator", "respond"): "正在组织回复…",
}


class StageStatus:
    def __init__(self):
        self.current = None

    def update(self, record):
        self.current = STAGE_LABELS.get((record.get("role"), record.get("phase")), "正在思考…")


STAGE_STATUS = StageStatus()


def build_pipeline() -> LearningPipeline:
    return create_pipeline(
        data_dir=DATA_DIR,
        public_base_url="http://localhost:8088", native_json=False,
        image_generator=limited_image,
        stage_observer=STAGE_STATUS.update,
    )


PIPELINE = build_pipeline()


def iframe_for(document: str) -> str:
    escaped = document.replace("&", "&amp;").replace('"', "&quot;")
    return (
        '<iframe srcdoc="' + escaped + '" '
        'style="width:100%;height:460px;border:1px solid #c7d8e3;border-radius:12px;'
        'background:#fff" sandbox="allow-scripts"></iframe>'
    )


def seen_before(device) -> bool:
    """The one thing the browser remembers: that it has met the coach before.

    A bare flag with no learning content; it only skips the first-meeting
    questions. Everything else is scoped to the open page's session.
    """
    return device is True


async def respond(user_text, session, device=None, allowed=False):
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
        STAGE_STATUS.current = None
        # No learner_id: learning records are keyed by this page's session and
        # die with it. seen only skips the first-meeting questions.

        def render_final(sess, output, new_state):
            """Publish the validated reply: chat entry, choices, panel, records."""
            sess["state"] = new_state
            text = learner_text(output)
            # Keep model context separate from presentation-only markup and image bytes.
            history_text = ARTIFACT_LINK.sub("", text).rstrip()
            panel = sess["panel"]
            tokens = ARTIFACT_LINK.findall(text)
            text = ARTIFACT_LINK.sub("", text).rstrip()
            for token in tokens:
                document = PIPELINE.artifacts.get(token)
                if not document:
                    continue
                image = DATA_IMAGE.search(document)
                if image:
                    # Generated picture (illustration or question picture): inline in chat.
                    text = f"{text}\n\n![配图]({image.group(0)})"
                else:
                    # Interactive visual: embedded sandboxed frame below chat.
                    panel = iframe_for(document)
            sess["history"].append({"role": "assistant", "text": history_text})
            sess["options"] = options_from(output)
            sess["panel"] = panel
            sess["chat"] = sess["chat"] + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": text},
            ]
            return panel

        streamer = getattr(PIPELINE, "run_stream", None)
        if streamer is not None:
            # Streaming: each delta replaces the placeholder, so the reply text
            # appears as it is generated; the final event is the validated reply.
            partial = ""
            async for event in streamer(list(session["history"]), session_id=session["session_id"],
                                        state=session["state"], learner_id=None,
                                        returning_visitor=seen_before(device)):
                if "delta" in event:
                    partial += event["delta"]
                    yield session, session["chat"] + [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": partial},
                    ], session["panel"], "", device
                    continue
                device = True
                panel = render_final(session, event["output"], event["state"])
                yield session, session["chat"], panel, "", device
            return
        # No streaming support: poll the stage observer while awaiting the turn.
        task = asyncio.create_task(PIPELINE.run(list(session["history"]), session_id=session["session_id"],
                                                state=session["state"], learner_id=None,
                                                returning_visitor=seen_before(device)))
        deadline = time.monotonic() + 240
        shown = None
        while True:
            try:
                output, new_state = await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                break
            except TimeoutError:
                if time.monotonic() > deadline:
                    task.cancel()
                    raise
                label = STAGE_STATUS.current
                if label and label != shown:
                    shown = label
                    thinking = session["chat"] + [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": label},
                    ]
                    yield session, thinking, session["panel"], "", device
        device = True
        panel = render_final(session, output, new_state)
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
    """Start the demo from a clean slate: drop this session's records and artifacts."""
    for remove, key in ((PIPELINE.store.clear, session.get("session_id", "")),
                        (PIPELINE.artifacts.delete_session, session.get("session_id", ""))):
        try:
            remove(key)
        except Exception:
            pass
    fresh = new_session()
    # The bare "visited before" flag is the only thing kept in the browser.
    return (*fresh, device)


def new_session():
    session = {"session_id": uuid.uuid4().hex, "state": None, "history": [],
               "chat": [], "panel": "", "locked": False}
    return session, [], "", ""


# ---------------------------------------------------------------- learning notes view

PROFILE_NAMES = {"learning_stage": "学段", "age_band": "年龄段", "learning_goal": "学习目标",
                 "prior_knowledge": "已有基础", "preferred_language": "语言", "available_time": "可用时间",
                 "accessibility_needs": "需要的帮助", "companion_role": "陪伴者", "preferred_method": "喜欢的讲法",
                 "learning_context": "学习场景"}
from learning_coach.domain.note_contracts import ERROR_LABELS as ERROR_NAMES  # noqa: E402
NO_NOTES = "还没有整理过笔记。做完一组题后选“整理”，或者随时在对话里说“整理笔记”。"


def mastery_words(item) -> str:
    level, attempts = item.get("mastery_level"), item.get("attempts") or 0
    if level is None or not attempts:
        return "还没做过题"
    status = "还不熟" if level < LEARNING else "正在巩固" if level < SOLID else "掌握得不错"
    return f"{status}（做过 {attempts} 题，对 {item.get('correct_count') or 0} 题）"


def render_notes(notes) -> str:
    profile = notes.get("profile") or {}
    concepts = notes.get("concepts") or []
    errors = notes.get("error_records") or []
    review = notes.get("review_plan") or []
    if not (profile or concepts or errors or review):
        return NO_NOTES
    labels = {item.get("concept_id"): item.get("label") or item.get("concept_id") for item in concepts}
    lines = []
    if profile:
        lines += ["**关于我**"] + [f"- {PROFILE_NAMES.get(key, key)}：{value.get('value', '')}"
                                   for key, value in profile.items() if isinstance(value, dict)]
    if concepts:
        ordered = sorted(concepts, key=lambda item: (item.get("mastery_level") is None, item.get("mastery_level") or 0))
        lines += ["", "**学过的知识点**"] + [f"- {labels[item.get('concept_id')]}：{mastery_words(item)}"
                                         for item in ordered]
    if errors:
        lines += ["", "**容易错的地方**"] + [
            f"- {labels.get(item.get('concept_id'), item.get('concept_id'))}：{ERROR_NAMES.get(item.get('category'), '原因待确认')}"
            for item in errors[-10:]]
    if review:
        lines += ["", "**复习安排**"] + [f"- {item.get('topic')}" + (f"（{item['deadline']}）" if item.get("deadline") else "")
                                     for item in review]
    return "\n".join(lines).strip()


def notes_markdown(session, allowed=False) -> str:
    """The learning notes compiled in this open session; they die with it."""
    if not allowed:
        return LOCKED_TEXT
    try:
        if not PIPELINE.store.is_enabled(session.get("session_id", "")):
            return "还没有开启学习记录，所以没有笔记。想保存，可以在对话里说“开启记录”。"
        return render_notes(PIPELINE.store.snapshot(session.get("session_id", "")))
    except Exception:
        return "笔记暂时读取不了，请稍后再试。"


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
                    gr.Markdown("每次打开页面都是一个新会话，不会保留之前的对话；本页开启的学习记录也只到本页关闭为止。"
                                "第一次使用时会问学段和是否保存学习记录；之后随时可以说“开启记录”“关闭记录”“删除记录”。")
            with gr.Accordion("我的笔记", open=False):
                show_notes = gr.Button("查看最新笔记", size="sm")
                notes_view = gr.Markdown("")
        session = gr.State(new_session()[0])
        # The only thing kept in this visitor's browser storage is a bare
        # "visited before" flag; it survives "新会话" and reloads.
        key = browser_secret()
        device = gr.BrowserState(False, storage_key="edu-agent-seen", secret=key)
        # Server-side per-visitor flag; set from the link on page load.
        allowed = gr.State(not ACCESS_KEY)

        async def on_send(text, sess, dev, ok):
            # respond is an async generator: first yields the "我先想想…"
            # placeholder, then the completed turn.
            async for result in respond(text, sess, dev, ok):
                yield result

        outputs = [session, chatbot, panel, input_box, device]

        def after_turn(event):
            # Show the answer's numbered choices as buttons.
            event.then(option_updates, [session], option_buttons)

        after_turn(send.click(on_send, [input_box, session, device, allowed], outputs))
        after_turn(input_box.submit(on_send, [input_box, session, device, allowed], outputs))
        for button in option_buttons:
            # A button passes its own label, so clicking it sends the chosen option.
            after_turn(button.click(on_send, [button, session, device, allowed], outputs))
        reset.click(new_session, None, [session, chatbot, panel, input_box]).then(
            option_updates, [session], option_buttons)
        if DEMO_STAGE:
            clear.click(clear_demo, [session, device], [session, chatbot, panel, input_box, device]).then(
                option_updates, [session], option_buttons)
        show_notes.click(notes_markdown, [session, allowed], [notes_view])
        demo.load(check_access, None, [allowed, main, locked_note])
        # Every page load starts a brand-new session. Gradio can re-attach a
        # reloaded page to its previous server-side session (the browser keeps
        # the session id); replacing it here drops the old chat, coach state
        # and learning records so nothing from the previous page-open survives.
        demo.load(new_session, None, [session, chatbot, panel, input_box]).then(
            option_updates, [session], option_buttons)
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 8000)))
