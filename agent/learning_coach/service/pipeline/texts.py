"""Fixed wording the program shows, and the replies it recognizes.

When each of these is used is decided by the program (router.py); everything
else a learner reads is written by the model.
"""

from learning_coach.domain.research_contracts import Evidence
from learning_coach.domain.routing import INTENT_UNCLEAR
from learning_coach.service.execution import Capability

RESPONSE_VERSION = "1.0"

# Record commands. Plain-language words map onto the program commands.
RECORD_ON = ("开启记录", "打开记录", "开启学习记录", "保存记录", "保存学习记录")
RECORD_OFF = ("关闭记录", "关掉记录", "暂停记录", "别记了", "不要记录", "关闭学习记录")
RECORD_DELETE = ("删除记录", "删掉记录", "删除学习记录", "清除记录", "清空记录")
CMD_MEMORY_ON = "/memory on"
CMD_MEMORY_OFF = "/memory off"
CMD_FORGET = "/forget"
CONFIRM_DELETE = "确定删除"

# Notes are compiled after a practice set, only when the learner agrees, or on request.
NOTES_OFFER = "要把这次学的整理成笔记吗？\n1. 整理\n2. 不用"
NOTES_REQUEST = ("整理笔记", "帮我整理笔记", "整理一下笔记", "生成笔记", "做笔记")
NOTES_YES = ("1", "整理", "整理吧", "整理一下", "好", "好的", "要", "可以")
NOTES_NO = ("2", "不用", "不用了", "先不用", "不要", "不整理")

# After an explanation the reply only offers a check question; the graded
# card appears when the learner takes the offer (a button in the demo page).
CHECK_OFFER = "想做一道小题，检验一下刚才这块吗？\n1. 做一道小题\n2. 先不用"
CHECK_YES = ("1", "做一道小题", "做一道", "做题", "做", "好", "好的", "可以", "来吧", "来", "考考我", "出题")
CHECK_NO = ("2", "先不用", "不用", "不用了", "不要", "不做", "不想做")

# While a question waits: replace it without grading (e.g. a picture that does not fit).
SKIP_QUESTION = ("换一题", "换一道", "换一道题", "跳过这题", "这题跳过", "换个题")

# The external-access question: the choices shown and the answers accepted come from one place.
ALLOW_EXTERNAL = "允许本次外部访问"
DECLINE_EXTERNAL = "先不联网，继续讲解"
EXTERNAL_YES = (ALLOW_EXTERNAL, "1")
EXTERNAL_NO = (DECLINE_EXTERNAL, "2")
EXTERNAL_QUESTION = f"是否允许本次外部访问？\n1. {ALLOW_EXTERNAL}\n2. {DECLINE_EXTERNAL}"
EXTERNAL_LABELS = {Capability.RESEARCH: "联网查资料", Capability.IMAGE_SEARCH: "联网搜索图片",
                   Capability.IMAGE_GENERATION: "调用外部生图服务", Capability.MCP: "通过 MCP 调用外部服务"}

# Links the program appends after the reply.
VISUAL_LINK = "打开互动图"
QUIZ_PICTURE_LINK = "看题目配图"
VISUAL_FAILED = "这次未生成可用的互动图。"
RESEARCH_NOTES = {Evidence.UNAVAILABLE: "本次未取得可核查资料，未完成检索验证。",
                  Evidence.LIKELY: "目前证据有限，尚未完成独立来源交叉核实。",
                  Evidence.DISPUTED: "来源存在冲突，相关说法不能当作确定结论。"}

# What the responder is told when a visual was not published (data for the
# coordinator, never the reviewer's notes to the designer).
VISUAL_FAILURE_REASONS = {"quality_not_accepted": "画了两稿，都没通过独立审阅",
                          "visual_timeout": "画图超过了这一轮的时间上限",
                          "insufficient_evidence": "审阅认为依据不足，没有发布",
                          "generation_unavailable": "画图服务这次不可用",
                          "review_unavailable": "审图服务这次不可用",
                          "render_unavailable": "图示渲染这次不可用"}
VISUAL_FAILED_REASON = "这次没画成"

NEXT_TOPIC = "接下来想学什么？"
# The program's introduction when a first message already asks something.
INTRO = "你好，我是你的学习教练，可以讲概念、陪你解题、出小题看看是不是学会了，需要时还能画图。"
INTRO_RECORDS_OFF = "学习记录默认不保存，想保存可以说“开启记录”。"
INTRO_RECORDS_ON = "学习记录已开启，想关掉可以说“关闭记录”。"
STEP_QUESTION = "question"   # next_step.kind when the reply asks something
CHOICE_LETTERS = "ABCD"      # how the choices of a question are labelled


def question(text: str) -> dict:
    """A next_step that asks the learner something."""
    return {"kind": STEP_QUESTION, "text": text}


def reply(message, primary=INTENT_UNCLEAR, step=None) -> dict:
    """A complete public response written by the program."""
    return {"schema_version": RESPONSE_VERSION, "intent": {"primary": primary, "is_implicit": False},
            "message": message, "next_step": step}


def render_question(quiz_question) -> str:
    """The program renders exactly one unanswered question, with no answer key."""
    choices = "\n".join(f"{CHOICE_LETTERS[i]}. {choice}" for i, choice in enumerate(quiz_question["choices"]))
    return quiz_question["question"] + "\n" + choices
