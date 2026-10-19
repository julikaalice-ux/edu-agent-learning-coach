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
import base64
from contextlib import aclosing, suppress
import hmac
from html import escape
import json
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

try:  # 部署环境（Space）有 fastapi；本地测试环境没有，不影响 Gradio 路径
    from fastapi import Request as FastAPIRequest
except ImportError:  # pragma: no cover
    FastAPIRequest = None

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
    ("image_search", "search"): "已找到候选图片，正在挑选…",
    ("image", "generate"): "配图已生成，正在整理…",
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


def iframe_for(document: str, token: str = "") -> str:
    escaped = escape(document, quote=True)
    # Picture pages (gallery) are taller than a single drawn visual.
    height = 640 if 'id="gallery"' in document else 460
    return (
        f'<details class="coach-visual" data-artifact="{escape(token, quote=True)}" open>'
        '<summary>互动演示 <span class="when-open">收起演示 ×</span>'
        '<span class="when-closed">重新打开演示</span></summary>'
        '<iframe title="互动演示" srcdoc="' + escaped + '" '
        f'style="width:100%;height:{height}px;border:1px solid #c7d8e3;border-radius:12px;'
        'background:#fff" sandbox="allow-scripts"></iframe></details>'
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
        yield session, session["chat"], gr.skip(), "", device
        return
    if session.get("locked"):
        yield session, session["chat"], gr.skip(), "", device
        return
    # Checked here, not only by hiding the page, so direct API calls are refused too.
    if not allowed or not TURN_LIMIT.take():
        session["chat"] = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": LIMIT_TEXT if allowed else LOCKED_TEXT},
        ]
        yield session, session["chat"], gr.skip(), "", device
        return
    session["locked"] = True
    task = None
    initial_explanation = ""
    final_published = False
    messages = session["history"] + [{"role": "user", "text": user_text}]
    try:
        # First token: acknowledge immediately so the UI never looks frozen.
        thinking = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "我先想想…"},
        ]
        yield session, thinking, gr.skip(), "", device
        STAGE_STATUS.current = None
        # No learner_id: learning records are keyed by this page's session and
        # die with it. seen only skips the first-meeting questions.

        def render_final(sess, output, new_state):
            """Publish the validated reply: chat entry, choices, panel, records."""
            sess["state"] = new_state
            text = learner_text(output)
            # Keep model context separate from presentation-only markup and image bytes.
            history_text = ARTIFACT_LINK.sub("", text).rstrip()
            # Leave the browser's collapse state and running iframe untouched
            # during progress updates and replies that do not bring a new visual.
            panel = gr.skip()
            tokens = ARTIFACT_LINK.findall(text)
            text = ARTIFACT_LINK.sub("", text).rstrip()
            for token in tokens:
                document = PIPELINE.artifacts.get(token)
                if not document:
                    continue
                image = DATA_IMAGE.search(document)
                # A picture page (AI picture, question picture) has no script;
                # a gallery also carries pictures but is an interactive page.
                if image and "<script" not in document:
                    # Generated picture (illustration or question picture): inline in chat.
                    text = f"{text}\n\n![配图]({image.group(0)})"
                elif token != sess.get("panel_token"):
                    # Only a new artifact opens/replaces the interactive panel.
                    panel = iframe_for(document, token)
                    sess["panel"] = panel
                    sess["panel_token"] = token
            sess["history"] = messages + [{"role": "assistant", "text": history_text}]
            sess["options"] = options_from(output)
            sess["chat"] = sess["chat"] + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": text},
            ]
            return panel

        streamer = getattr(PIPELINE, "run_stream", None)
        if streamer is not None:
            # Multi-agent turns can publish a validated explanation before the
            # visual is ready. Single-agent token deltas use the same bubble;
            # only the final event commits conversation state and artifacts.
            partial = ""
            async with aclosing(streamer(messages, session_id=session["session_id"],
                                         state=session["state"], learner_id=None,
                                         returning_visitor=seen_before(device))) as stream:
                async for event in stream:
                    if "explanation" in event:
                        initial_explanation = event["explanation"]
                        yield session, session["chat"] + [
                            {"role": "user", "content": user_text},
                            {"role": "assistant", "content": initial_explanation},
                        ], gr.skip(), "", device
                        continue
                    if "delta" in event:
                        partial += event["delta"]
                        yield session, session["chat"] + [
                            {"role": "user", "content": user_text},
                            {"role": "assistant", "content": partial},
                        ], gr.skip(), "", device
                        continue
                    device = True
                    panel = render_final(session, event["output"], event["state"])
                    final_published = True
                    yield session, session["chat"], panel, "", device
                    return
            return
        # No streaming support: poll the stage observer while awaiting the turn.
        task = asyncio.create_task(PIPELINE.run(messages, session_id=session["session_id"],
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
                    yield session, thinking, gr.skip(), "", device
        device = True
        panel = render_final(session, output, new_state)
        final_published = True
        yield session, session["chat"], panel, "", device
    except Exception:
        if final_published:
            return
        failure = "这次处理没有顺利完成，请再试一次。"
        if initial_explanation:
            failure = f"{initial_explanation}\n\n后续内容暂时没有完成，请稍后重试。"
        session["chat"] = session["chat"] + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": failure},
        ]
        yield session, session["chat"], gr.skip(), "", device
    finally:
        # Reset cancels the generator; shield must not leave its pipeline running.
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
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
               "chat": [], "panel": "", "panel_token": None, "locked": False}
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


COACH_CSS = """
/* 学习教练 · edu-agent 演示页样式：自然元素色系（暖纸底 + 苔绿 + 鼠尾草）。
   配色参考视觉参考库「打开画册」：底 #f3f1eb、主文字 #202721、链接绿 #2c6040、
   辅助灰绿 #62715f、标签底 #e4eadf、卡片描边 #d9ded3、控件描边 #bbc5b5。 */
.gradio-container { max-width: 880px !important; margin: 0 auto !important;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei",
        "Noto Sans SC", sans-serif !important; }
body, .gradio-container { background: #f3f1eb !important; }

/* 页头 */
.coach-hero { text-align: center; padding: 26px 16px 10px; }
.coach-hero-emoji { font-size: 44px; line-height: 1; margin-bottom: 8px; }
.coach-hero h1 { font-size: 1.9em; margin: 0 0 6px; color: #202721; letter-spacing: .02em; }
.coach-hero p { margin: 0 0 14px; color: #62715f; font-size: .98em; }
.coach-hero-chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-bottom: 10px; }
.coach-hero-chips span { background: #e4eadf; color: #2c6040;
    border-radius: 6px; padding: 4px 12px; font-size: .86em; }
.coach-hero-note { color: #62715f; font-size: .82em; }

/* 对话气泡 */
#coach-chat { border: 1px solid #d9ded3; border-radius: 12px; background: #fff;
    box-shadow: 0 4px 18px rgba(32,39,33,.06); padding: 8px; }
#coach-chat .message { gap: 8px; }
#coach-chat .message .bubble-wrap { border-radius: 14px; padding: 10px 14px; line-height: 1.7; }
#coach-chat .message.user .bubble-wrap, #coach-chat [data-testid="user"] .bubble-wrap {
    background: linear-gradient(135deg, #2c6040, #264f36); color: #fff;
    border-bottom-right-radius: 6px; }
#coach-chat .message.bot .bubble-wrap, #coach-chat [data-testid="bot"] .bubble-wrap {
    background: #faf9f5; color: #202721; border: 1px solid #d9ded3;
    border-bottom-left-radius: 6px; }
#coach-chat .message.user, #coach-chat [data-testid="user"] { justify-content: flex-end; }
#coach-chat .message.bot, #coach-chat [data-testid="bot"] { justify-content: flex-start; }

/* 选择按钮 */
#coach-choices { gap: 8px; flex-wrap: wrap; }
#coach-choices button { border-radius: 6px !important; background: #e4eadf !important;
    border: 1px solid #c8d6c5 !important; color: #2c6040 !important;
    padding: 6px 16px !important; font-size: .9em !important; min-height: 34px; }
#coach-choices button:hover { background: #d8e4d2 !important; border-color: #9db79a !important; }

/* 输入行 */
#coach-composer { gap: 10px; align-items: end; }
#coach-composer input, #coach-composer textarea {
    border-radius: 8px !important; border: 1px solid #bbc5b5 !important;
    padding: 12px 16px !important; background: #fff !important;
    box-shadow: 0 1px 4px rgba(32,39,33,.05); }
#coach-composer input:focus, #coach-composer textarea:focus { border-color: #63836d !important;
    box-shadow: 0 0 0 3px rgba(99,131,109,.18) !important; }
#coach-send, #coach-send button { border-radius: 8px !important; min-height: 44px !important;
    padding: 0 24px !important; font-weight: 600; }

/* 底部工具栏与说明 */
#coach-toolbar { align-items: center; }
#coach-reset, #coach-clear { border-radius: 6px !important; }
.coach-footnote { color: #62715f !important; font-size: .82em; margin: 0; text-align: right; }

/* 笔记面板 */
#coach-notes { border: 1px solid #d9ded3 !important; border-radius: 12px !important;
    background: #fff; box-shadow: 0 2px 10px rgba(32,39,33,.05); }

/* 焦点可见性（参考库同款） */
button:focus-visible, input:focus-visible, textarea:focus-visible, summary:focus-visible {
    outline: 3px solid #63836d; outline-offset: 2px; }

/* 移动端 */
@media (max-width: 640px) {
    .coach-hero { padding-top: 16px; }
    .coach-hero h1 { font-size: 1.5em; }
    .coach-footnote { text-align: left; }
    #coach-chat { height: 60vh !important; }
}
"""


def build_app() -> gr.Blocks:
    theme = gr.themes.Soft(
        primary_hue="blue", secondary_hue="teal", neutral_hue="slate",
        radius_size="lg",
    )
    with gr.Blocks(title="学习教练 · edu-agent", theme=theme, css=COACH_CSS) as demo:
        gr.HTML(
            '<div class="coach-hero">'
            '<div class="coach-hero-emoji">🎓</div>'
            '<h1>学习教练 · edu-agent</h1>'
            '<p>多 agent 学习教练：意图编排 · 检索查证 · 出题判分 · 学习记忆 · 互动图与配图</p>'
            '<div class="coach-hero-chips">'
            '<span>💡 讲概念</span><span>✍️ 陪解题</span><span>🧩 出小题</span><span>🖼️ 画图</span>'
            '<span>🔍 查资料</span></div>'
            '<div class="coach-hero-note">Azure AI Foundry 驱动 · 无需登录即可体验</div>'
            "</div>"
        )
        locked_note = gr.Markdown(f"**{LOCKED_TEXT}**", visible=False)
        with gr.Column(visible=not ACCESS_KEY) as main:
            chatbot = gr.Chatbot(label="", height=540, autoscroll=False,
                                 elem_id="coach-chat", placeholder=
                                 "你好呀 👋 我是你的学习教练。\n\n"
                                 "可以直接问我：\n"
                                 "· 帮我解释一下什么是光合作用\n"
                                 "· 给我出几道分数加减法的题\n"
                                 "· 画一个对比 1/2 和 1/4 的互动图")
            panel = gr.HTML("", elem_id="coach-panel", css_template="""
                .coach-visual { border: 1px solid #dbe7ee; border-radius: 16px; overflow: hidden;
                                background: #fff; box-shadow: 0 2px 12px rgba(24,51,75,.06); }
                summary { cursor: pointer; padding: 12px 16px; font-weight: 600; color: #18334b; }
                summary span { float: right; font-size: 0.9em; font-weight: 400; color: #6b8394; }
                details[open] .when-closed, details:not([open]) .when-open { display: none; }
                iframe { display: block; max-width: 100%; border: none; }
            """)
            with gr.Row(elem_id="coach-choices"):
                option_buttons = [gr.Button(visible=False, size="sm") for _ in range(MAX_OPTIONS)]
            with gr.Row(elem_id="coach-composer"):
                input_box = gr.Textbox(
                    placeholder="问点什么，比如：帮我生成一个对比 1/2 和 1/4 的互动图",
                    scale=9, label="", container=False, lines=1, max_lines=5)
                send = gr.Button("发送", scale=1, variant="primary", elem_id="coach-send")
            with gr.Row(elem_id="coach-toolbar"):
                reset = gr.Button("新会话", size="sm", elem_id="coach-reset")
                if DEMO_STAGE:
                    clear = gr.Button("清空演示数据", size="sm", elem_id="coach-clear")
                    gr.Markdown(f"<div class='coach-footnote'>演示模式：预设学段「{DEMO_STAGE}」，学习记录已开启。</div>")
                else:
                    gr.Markdown("<div class='coach-footnote'>每次打开页面都是一个新会话，不会保留之前的对话；"
                                "本页开启的学习记录也只到本页关闭为止。第一次使用时会问学段和是否保存学习记录，"
                                "之后随时可以说「开启记录」「关闭记录」「删除记录」。</div>")
            with gr.Accordion("我的笔记", open=False, elem_id="coach-notes"):
                show_notes = gr.Button("查看最新笔记", size="sm")
                notes_view = gr.Markdown("")
        session = gr.State(new_session()[0])
        # The only thing kept in this visitor's browser storage is a bare
        # "visited before" flag; it survives "新会话" and reloads.
        key = browser_secret()
        device = gr.BrowserState(False, storage_key="edu-agent-seen", secret=key)
        # Server-side per-visitor flag; set from the link on page load.
        allowed = gr.State(not ACCESS_KEY)

        # Bind the generator directly so Gradio cancellation closes it promptly.
        on_send = respond

        outputs = [session, chatbot, panel, input_box, device]

        turn_events = []

        def after_turn(event):
            turn_events.append(event)
            # Show the answer's numbered choices as buttons.
            event.then(option_updates, [session], option_buttons)

        after_turn(send.click(on_send, [input_box, session, device, allowed], outputs))
        after_turn(input_box.submit(on_send, [input_box, session, device, allowed], outputs))
        for button in option_buttons:
            # A button passes its own label, so clicking it sends the chosen option.
            after_turn(button.click(on_send, [button, session, device, allowed], outputs))
        reset.click(new_session, None, [session, chatbot, panel, input_box],
                    cancels=turn_events, queue=False).then(
            option_updates, [session], option_buttons)
        if DEMO_STAGE:
            clear.click(clear_demo, [session, device], [session, chatbot, panel, input_box, device],
                        cancels=turn_events, queue=False).then(
                option_updates, [session], option_buttons)
        show_notes.click(notes_markdown, [session, allowed], [notes_view])
        # Loaded from a local asset; no browser network service is needed.
        demo.load(fn=None, js=Path(__file__).with_name("ui.js").read_text(encoding="utf-8"))
        demo.load(check_access, None, [allowed, main, locked_note])
        # Every page load starts a brand-new session. Gradio can re-attach a
        # reloaded page to its previous server-side session (the browser keeps
        # the session id); replacing it here drops the old chat, coach state
        # and learning records so nothing from the previous page-open survives.
        demo.load(new_session, None, [session, chatbot, panel, input_box]).then(
            option_updates, [session], option_buttons)
    return demo


class TurnTimer:
    """Milliseconds from the start of one /turn to each of its steps, logged once.

    Only step names and times reach the log, never the question or the reply:
    token_ms (Azure token; cached after the first), new_conversation_ms (only
    for a new chat), first_text_ms (first reply text from the agent), total_ms.
    """

    def __init__(self):
        self.started = time.perf_counter()
        self.marks = {}

    def mark(self, name):
        self.marks.setdefault(name, round((time.perf_counter() - self.started) * 1000))

    def log(self, outcome):
        self.mark("total_ms")
        logger.info("turn_timing outcome=%s %s", outcome,
                    " ".join(f"{name}={value}" for name, value in self.marks.items()))


def page_picture(document) -> tuple[str, bytes] | None:
    """(media type, bytes) of the picture embedded in an artifact page, or None."""
    found = DATA_IMAGE.search(document or "")
    if found is None:
        return None
    media_type, data = found.group(0)[len("data:"):].split(";base64,", 1)
    return media_type, base64.b64decode(data)


PROXY_CODE = re.compile(r"[a-z][a-z0-9_]{2,60}")


def proxy_error(error) -> str:
    """What the page may show about a failed turn: the proxy's own code
    (e.g. service_principal_token_http_401, agent_http_403) or else the error
    type; never an error message, which can carry request details."""
    if isinstance(error, RuntimeError) and PROXY_CODE.fullmatch(str(error)):
        return str(error)
    return type(error).__name__


def run_combined(port: int) -> None:
    """同一容器服务三样：新前端（根路径静态）、/turn 代理、原 Gradio（/legacy 备用）。

    前端构建产物在 ui/（build_space 复制 frontend/dist），代理模块是
    同目录的 proxy.py（build_space 从 backend/ 复制）。
    """
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn

    import proxy as space_proxy  # noqa: E402  (build_space 复制到本目录)

    ui_dir = Path(__file__).with_name("ui")
    fastapi_app = FastAPI()

    # 前端首页与静态资源先注册（优先于后面的 Gradio 挂载）；
    # Gradio 挂根路径，保证平台健康检查的 /config、/gradio_api/* 可用。
    @fastapi_app.get("/")
    async def index():
        if ui_dir.is_dir():
            return FileResponse(ui_dir / "index.html")
        return JSONResponse(status_code=404, content={"error": "ui_not_built"})

    if (ui_dir / "assets").is_dir():
        fastapi_app.mount("/assets", StaticFiles(directory=str(ui_dir / "assets")), name="ui-assets")

    @fastapi_app.post("/turn")
    async def turn(request: FastAPIRequest):
        payload = await request.json()
        if not TURN_LIMIT.take():
            return JSONResponse(status_code=200, content={
                "conversation": None, "text": LIMIT_TEXT, "options": []})
        user_input = str(payload.get("input", "")).strip()
        if not user_input:
            return JSONResponse(status_code=400, content={"error": "empty_input"})
        conversation = payload.get("conversation")
        timer = TurnTimer()
        try:
            await asyncio.to_thread(space_proxy.get_token)
        except Exception as error:
            timer.log("token_failed")
            return JSONResponse(status_code=502, content={"error": proxy_error(error)})
        timer.mark("token_ms")
        if not conversation:
            try:
                conversation = space_proxy.call("conversations", {}).get("id")
            except Exception as error:
                timer.log("conversation_failed")
                return JSONResponse(status_code=502, content={"error": proxy_error(error)})
            if not conversation:
                timer.log("conversation_failed")
                return JSONResponse(status_code=502, content={"error": "conversation_create_failed"})
            timer.mark("new_conversation_ms")
        try:
            if payload.get("stream"):
                async def events():
                    outcome = "failed"
                    try:
                        for event in space_proxy.stream_reply(conversation, user_input):
                            if "delta" in event:
                                timer.mark("first_text_ms")
                                frame = json.dumps({"delta": event["delta"]}, ensure_ascii=False)
                            else:
                                frame = json.dumps({"done": True, "conversation": event["conversation"],
                                                    "text": event["text"],
                                                    "options": space_proxy.extract_options(event["text"])},
                                                   ensure_ascii=False)
                            yield f"data: {frame}\n\n"
                        outcome = "ok"
                    finally:
                        timer.log(outcome)
                return StreamingResponse(events(), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache"})
            result = space_proxy.call("responses", {"conversation": conversation, "input": user_input})
            timer.mark("first_text_ms")
            text = space_proxy.extract_text(result) or "这次处理没有顺利完成，请再试一次。"
            timer.log("ok")
            return JSONResponse(status_code=200, content={
                "conversation": conversation, "text": text,
                "options": space_proxy.extract_options(text)})
        except Exception as error:
            timer.log("failed")
            return JSONResponse(status_code=502, content={"error": proxy_error(error)})

    # Visuals from this container's own pipeline (legacy UI), or else the ones
    # the hosted agent published; a few recent pages are kept so the iframe and
    # the picture route do not fetch the same page twice.
    fetched = {}

    async def artifact_document(token: str) -> str | None:
        document = PIPELINE.artifacts.get(token)
        if document is None and token in fetched:
            document = fetched[token]
        if document is None:
            document = await asyncio.to_thread(space_proxy.fetch_artifact, token)
            if document is not None:
                fetched[token] = document
                while len(fetched) > 16:
                    fetched.pop(next(iter(fetched)))
        return document

    @fastapi_app.get("/artifact/{token}")
    async def artifact(token: str):
        try:
            document = await artifact_document(token)
        except Exception as error:
            return JSONResponse(status_code=502, content={"error": proxy_error(error)})
        if document is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        # No X-Frame-Options (see proxy.ARTIFACT_POLICY): opened through
        # huggingface.co or modelscope.cn, this page is itself in a frame of
        # another site, and a same-origin rule left the visual blank.
        return HTMLResponse(document, headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": space_proxy.ARTIFACT_POLICY,
        })

    @fastapi_app.get("/artifact/{token}/image")
    async def artifact_image(token: str):
        """The picture inside a question-picture page, for an <img> in the chat."""
        try:
            document = await artifact_document(token)
        except Exception as error:
            return JSONResponse(status_code=502, content={"error": proxy_error(error)})
        picture = page_picture(document)
        if picture is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return Response(picture[1], media_type=picture[0], headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    # 最后挂 Gradio 到根路径：平台健康检查依赖 /config、/gradio_api/*，
    # 且它不能吞掉上面已注册的 /、/assets、/turn、/artifact。
    demo = build_app()
    gr.mount_gradio_app(fastapi_app, demo, path="/")

    uvicorn.run(fastapi_app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    if Path(__file__).with_name("ui").is_dir():
        run_combined(port)
    else:
        build_app().launch(server_name="0.0.0.0", server_port=port)
