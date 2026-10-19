"""Record commands (on, off, delete) and the first-meeting questions.

Both are program-only: no model decides whether a learner consented or
whether records are deleted.
"""

import logging

from learning_coach.domain import onboarding
from learning_coach.domain.contracts import MAX_QUESTION_CHARS
from learning_coach.domain.note_contracts import COMPANION_REPORT, SCHEMA_VERSION as NOTES_VERSION, SELF_REPORT
from learning_coach.domain.onboarding import (BY_GUARDIAN, NO_STAGE, RECORDS_OFF, RECORDS_ON, STEP_CONSENT_GUARDIAN,
                                              STEP_DONE, STEP_WHO, WHO_COMPANION, WHO_SELF)
from learning_coach.domain.routing import INTENT_CHAT, INTENT_UNCLEAR
from learning_coach.service.pipeline.shell import USER
from learning_coach.service.pipeline.state import Key
from learning_coach.service.pipeline.texts import (CMD_FORGET, CMD_MEMORY_OFF, CMD_MEMORY_ON, CONFIRM_DELETE,
                                                   RECORD_DELETE, question, reply)
from learning_coach.service.ports import StorageError

LOG = logging.getLogger(__name__)

# A short reply to a first-meeting question is an attempt to answer it; a
# longer one is more likely the learner's actual question.
QUESTION_MIN_CHARS = 7


def last_forget(messages) -> int:
    """Index of the latest confirmed deletion ("/forget", or "确定删除" right after a delete request)."""
    users = [(i, item["text"].strip()) for i, item in enumerate(messages) if item["role"] == USER]
    found = -1
    for position, (index, value) in enumerate(users):
        if value == CMD_FORGET or (value == CONFIRM_DELETE and position > 0 and users[position - 1][1] in RECORD_DELETE):
            found = index
    return found


class Commands:
    def __init__(self, deps, *, shell, ask_first: bool, demo_stage: str | None):
        self.deps, self.shell = deps, shell
        # ask_first: ask who is learning, their stage and record consent before
        # the first lesson. demo_stage: skip those questions with a preset stage
        # and records on, for demonstrations only.
        self.ask_first, self.demo_stage = ask_first, demo_stage

    # ------------------------------------------------------------ record commands

    @staticmethod
    def ask_delete(turn):
        turn.state[Key.PENDING_FORGET] = True
        return reply("要删除全部学习记录吗？删除后找不回来。", INTENT_CHAT,
                     question(f"确认删除请回复“{CONFIRM_DELETE}”，不删就继续说别的。")), turn.state

    def forget(self, turn):
        """Delete this learner's records and visuals; the session starts over with records off."""
        self.shell.forget_replays(turn.session_id)
        complete = True
        for remove, key in ((self.deps.store.clear, turn.memory_key), (self.deps.artifacts.delete_session, turn.session_id)):
            try:
                remove(key)
            except (OSError, StorageError):
                complete = False
                LOG.warning("forget_cleanup_incomplete")
        message = ("已删除本会话保存的学习笔记和可视化文件，并关闭记忆。客户端的聊天记录需在客户端删除。" if complete else
                   f"已清空当前学习任务并停止读取和记录笔记，但部分已存内容未能删除。请稍后重试 {CMD_FORGET}。")
        after = {Key.MEMORY_BLOCKED: True}
        record = turn.state.get(Key.ONBOARDING) or {}
        if record:
            after[Key.ONBOARDING] = {"step": STEP_DONE, "records": RECORDS_OFF, "stage": record.get("stage"),
                                     "who": record.get("who")}
        return reply(message, INTENT_CHAT), after

    def set_records(self, turn, enabled: bool):
        state = turn.state
        record = state.get(Key.ONBOARDING) or {}
        if (enabled and record.get("who") == WHO_SELF and record.get("stage") in onboarding.MAYBE_UNDER_14
                and record.get("consent_by") != BY_GUARDIAN):
            # A possibly under-14 learner cannot switch records on alone.
            state[Key.ONBOARDING] = {**record, "step": STEP_CONSENT_GUARDIAN}
            return reply("开启学习记录需要家长同意。", INTENT_UNCLEAR,
                         question(onboarding.question_text(STEP_CONSENT_GUARDIAN))), state
        if not enabled:
            state[Key.MEMORY_BLOCKED] = True
        try:
            self.deps.store.set_enabled(turn.memory_key, enabled)
        except StorageError:
            if enabled:
                return reply(f"未能开启记忆，本轮不会记录笔记。请稍后重试 {CMD_MEMORY_ON}。", INTENT_CHAT), state
            return reply(f"本会话已暂停读取和记录笔记，但持久开关未能更新。请稍后重试 {CMD_MEMORY_OFF}。", INTENT_CHAT), state
        state[Key.MEMORY_BLOCKED] = not enabled
        return reply("好，从现在开始保存学习进度。想关掉随时说“关闭记录”。" if enabled else
                     "好，不再记录学习进度。已经保存的内容想删掉，可以说“删除记录”。", INTENT_CHAT), state

    # ------------------------------------------------------------ first meeting

    def first_meeting(self, turn):
        """The first-meeting questions, an interrupt answered before any lesson.

        Returns a reply while they run. Returns None when the lesson goes on;
        then the turn may carry the question the learner arrived with and the
        closing words to put in front of its answer (turn.prefix).
        """
        state, text = turn.state, turn.text
        record = state.get(Key.ONBOARDING)
        if self.demo_stage and record is None:
            state[Key.ONBOARDING] = {"step": STEP_DONE, "stage": self.demo_stage, "records": RECORDS_ON,
                                     "who": WHO_SELF, "demo": True}
            try:
                self.deps.store.set_enabled(turn.memory_key, True)
                state[Key.MEMORY_BLOCKED] = False
            except StorageError:
                LOG.warning("demo_records_unavailable")
            return None
        if (self.ask_first and record is None and not turn.returning_visitor
                and not self.deps.store.is_enabled(turn.memory_key)):
            state[Key.ONBOARDING] = onboarding.start(text)
            note = ("你的问题我先记下了，回答两个小问题后马上讲。" if state[Key.ONBOARDING]["pending_question"]
                    else "开始之前，先问两个小问题，好按你的情况来讲。")
            greeting = ("你好！我是你的学习教练，可以给你讲概念、陪你解题、出小题看看是不是真的学会了，"
                        "需要的时候还能画图帮你理解。" + note)
            return reply(greeting, INTENT_UNCLEAR, question(onboarding.question_text(STEP_WHO)))
        if record is None or record.get("step") == STEP_DONE:
            return None
        updated = onboarding.advance(record, text)
        if updated.get("retry"):
            updated.pop("retry")
            if not updated.get("pending_question") and len(text) >= QUESTION_MIN_CHARS:
                updated["pending_question"] = text[:MAX_QUESTION_CHARS]
                message = "这个问题我先记下了，选完这一项马上讲。"
            else:
                message = "没认出这个选择，再选一次就好。"
            state[Key.ONBOARDING] = updated
            return reply(message, INTENT_UNCLEAR, question(onboarding.question_text(updated["step"])))
        if updated["step"] != STEP_DONE:
            state[Key.ONBOARDING] = updated
            return reply("好的。", INTENT_UNCLEAR, question(onboarding.question_text(updated["step"])))
        self._finish_first_meeting(turn, updated)
        pending = updated.pop("pending_question", None)
        updated.pop("stage_answer", None)
        state[Key.ONBOARDING] = updated
        closing = onboarding.closing_message(updated)
        if not pending:
            return reply(closing, INTENT_UNCLEAR, question("今天想弄懂点什么？"))
        # Answer the question the learner arrived with, as if just asked.
        turn.messages, turn.prefix = [{"role": USER, "text": pending}], closing
        return None

    def _finish_first_meeting(self, turn, record):
        """Apply the consent answer; a stated stage becomes the first profile entry."""
        records_on = record.get("records") == RECORDS_ON
        try:
            self.deps.store.set_enabled(turn.memory_key, records_on)
            if records_on and record.get("stage") not in (None, NO_STAGE) and record.get("stage_answer"):
                answer = record["stage_answer"]
                self.deps.store.apply_notes(turn.memory_key, f"{turn.session_id}:{turn.event_id}:stage", {
                    "schema_version": NOTES_VERSION, "concepts": [], "error_records": [], "review_plan": [],
                    "profile": [{"key": "learning_stage", "value": record["stage"],
                                 "origin": COMPANION_REPORT if record.get("who") == WHO_COMPANION else SELF_REPORT,
                                 "evidence_quote": answer}],
                }, answer)
        except (StorageError, ValueError, OSError):
            LOG.warning("onboarding_record_setting_failed")
            records_on = False
            record["records"] = RECORDS_OFF
        turn.state[Key.MEMORY_BLOCKED] = not records_on
