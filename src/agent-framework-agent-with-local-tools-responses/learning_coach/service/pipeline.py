"""Explicit routing, evidence handoffs, quiz progress and commit-after-validation."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import logging
import re

from learning_coach.domain.contracts import (QUIZ_SCHEMA, ContractError, check_question, decision_schema,
                                             public_quiz, selected_choice, validate)
from learning_coach.domain import onboarding
from learning_coach.domain.context import (Budgets, accept_notes, brief_card, count_tokens, empty_notes,
                                           learner_card, learner_messages, memory_slice, note_slice,
                                           number_turns, recent_window, research_digest)
from learning_coach.domain.note_contracts import ERROR_LABELS, NOTES_SCHEMA, SCHEMA_VERSION as NOTES_VERSION
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.domain.routing import (conservative_participant, empty_results, execution_order,
                      recover_explicit_visual_route,
                      recover_practice_route,
                      recover_quiz_route, require_finished, select_tools)
from learning_coach.service.execution import BUDGET, CALL_RESERVE, CallBudget, StageFailure
from learning_coach.service.visual_generation import VisualGenerationService
from learning_coach.service.ports import AgentRunner, ResearchPort, LearningMemory, Artifacts, ImageGenerator, StorageError


LOG = logging.getLogger(__name__)


RECORD_ON = ("开启记录", "打开记录", "开启学习记录", "保存记录", "保存学习记录")
RECORD_OFF = ("关闭记录", "关掉记录", "暂停记录", "别记了", "不要记录", "关闭学习记录")
RECORD_DELETE = ("删除记录", "删掉记录", "删除学习记录", "清除记录", "清空记录")
# Notes are compiled after a quiz set, only when the learner agrees, or on request.
NOTES_OFFER = "要把这次学的整理成笔记吗？\n1. 整理\n2. 不用"
NOTES_REQUEST = ("整理笔记", "帮我整理笔记", "整理一下笔记", "生成笔记", "做笔记")
NOTES_YES = ("1", "整理", "整理吧", "整理一下", "好", "好的", "要", "可以")
NOTES_NO = ("2", "不用", "不用了", "先不用", "不要", "不整理")
# While a question waits: replace it without grading (e.g. a picture that does not fit).
SKIP_QUESTION = ("换一题", "换一道", "换一道题", "跳过这题", "这题跳过", "换个题")
QUIZ_PICTURE_LINK = "看题目配图"
# Visible history accepted per turn (notes are compiled from the learner's
# words since the last compile); a safety cap only, not a context budget.
MAX_HISTORY = 400
MAX_MESSAGE_CHARS = 20000


def render_question(question) -> str:
    """The program renders exactly one unanswered question, with no answer key."""
    choices = "\n".join(f"{'ABCD'[i]}. {choice}" for i, choice in enumerate(question["choices"]))
    return question["question"] + "\n" + choices


def default_participant(state) -> str:
    """Who answers a new question when no plan says so: a companion only when one said so."""
    quiz = state.get("quiz") or {}
    if quiz.get("participant") in ("learner", "companion_report", "unknown") and not quiz.get("finished"):
        return quiz["participant"]
    return "companion_report" if (state.get("onboarding") or {}).get("who") == "companion" else "learner"


def empty_learning_notes():
    return {"schema_version": NOTES_VERSION, "profile": [], "concepts": [], "error_records": [], "review_plan": []}


def last_forget(messages):
    """Index of the latest confirmed deletion ("/forget", or "确定删除" right after a delete request)."""
    users = [(i, item["text"].strip()) for i, item in enumerate(messages) if item["role"] == "user"]
    found = -1
    for position, (index, value) in enumerate(users):
        if value == "/forget" or (value == "确定删除" and position > 0 and users[position - 1][1] in RECORD_DELETE):
            found = index
    return found


def reply(message, primary="G", step=None):
    return {"schema_version": "1.0", "intent": {"primary": primary, "is_implicit": False},
            "message": message, "next_step": step}


class LearningPipeline:
    def __init__(self, runner: AgentRunner, research: ResearchPort, store: LearningMemory,
                 artifacts: Artifacts, response_schema: dict, *,
                 image_generator: ImageGenerator | None = None,
                 onboarding: bool = False, demo_stage: str | None = None,
                 budgets: Budgets | None = None):
        self.runner, self.research = runner, research
        # Token budgets for what each agent sees (domain/context.py).
        self.budgets = budgets or Budgets()
        self.store, self.artifacts = store, artifacts
        self.response_schema = response_schema
        # Dependency inversion: the pipeline depends only on this callable
        # abstraction, never on a concrete image backend. Composition roots
        # (server.py / tests) inject pollinations, FLUX, or a fake.
        self.image_generator = image_generator
        self.decision_schema = decision_schema(response_schema)
        # onboarding: ask who is learning, their stage and record consent before
        # the first lesson. demo_stage: skip those questions with a preset stage
        # and records on, for demonstrations only.
        self.onboarding = onboarding
        self.demo_stage = demo_stage
        self._locks = {}
        self._recent = OrderedDict()
        self._accepted = OrderedDict()

    async def run(self, messages, *, session_id, state=None, turn_id=None,
                  learner_id=None, returning_visitor=False):
        """Run one turn.

        session_id identifies this conversation. learner_id is an optional,
        anonymous ID that the client keeps across conversations (for example in
        the browser); learning records are stored under it so a returning
        learner keeps their progress. Without it, records belong to the
        conversation only. returning_visitor is a client hint that this device
        has talked to the coach before; it only suppresses the first-meeting
        introduction and grants nothing else.
        """
        # IDs are supplied by the host/application, never by model output.
        if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
            raise ValueError("invalid_session_id")
        if learner_id is not None and (not isinstance(learner_id, str) or not learner_id.strip()
                                       or len(learner_id) > 200):
            raise ValueError("invalid_learner_id")
        memory_key = learner_id or session_id
        # Keep the visible history (notes are compiled from it); what each agent
        # actually sees is cut to token budgets later.
        clean = [{"role": m["role"], "text": m["text"][:MAX_MESSAGE_CHARS]} for m in messages[-MAX_HISTORY:]
                 if m.get("role") in ("user", "assistant") and isinstance(m.get("text"), str)]
        if not clean or clean[-1]["role"] != "user" or not clean[-1]["text"].strip():
            return reply("请告诉我你现在想学什么。", step={"kind": "question", "text": "你想从哪个问题开始？"}), deepcopy(state or {})
        # A history fingerprint distinguishes a transport replay from a real later answer.
        fingerprint = hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        event_id = turn_id or fingerprint
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            working = deepcopy(state or {})
            if working.get("owner_session", session_id) != session_id:
                raise ValueError("session_state_owner_mismatch")
            cached = self._recent.get(session_id)
            if cached and cached.get("revision", 0) >= working.get("revision", 0):
                working = deepcopy(cached)
            accepted = self._accepted.get((session_id, event_id))
            if accepted is not None:
                if accepted["input"] != fingerprint:
                    raise ValueError("turn_id_conflict")
                return deepcopy(accepted["response"]), working
            if working.get("last_event") == event_id and working.get("last_input") == fingerprint:
                return deepcopy(working["last_response"]), working
            if working.get("last_event") == event_id:
                raise ValueError("turn_id_conflict")
            committed_baseline = deepcopy(working)
            token = BUDGET.set(CallBudget())
            try:
                output, updated = await asyncio.wait_for(
                    self._execute(clean, session_id, working, event_id, memory_key,
                                  returning_visitor is True), timeout=180,
                )
            except (StageFailure, ContractError, TimeoutError, OSError, ValueError, StorageError):
                LOG.warning("learning_turn_failed")
                output = reply("这次处理没有顺利完成，学习记录和进度没有继续推进。",
                               step={"kind": "question", "text": "要从刚才的问题再试一次吗？"})
                return output, committed_baseline
            finally:
                BUDGET.reset(token)
            validate(output, self.response_schema)
            updated.update(last_event=event_id, last_input=fingerprint, last_response=deepcopy(output),
                           revision=working.get("revision", 0) + 1, owner_session=session_id)
            self._recent[session_id] = deepcopy(updated)
            self._accepted[(session_id, event_id)] = {"input": fingerprint, "response": deepcopy(output)}
            while len(self._accepted) > 2048:
                self._accepted.popitem(last=False)
            self._recent.move_to_end(session_id)
            while len(self._recent) > 512:
                old_id, _ = self._recent.popitem(last=False)
                old_lock = self._locks.get(old_id)
                if old_lock is not None and not old_lock.locked():
                    self._locks.pop(old_id, None)
            # One application event from the accepted public response. No raw learner text.
            LOG.info("intent_result %s", json.dumps({"turn": hashlib.sha256(event_id.encode()).hexdigest(),
                     "intent": output["intent"]}, ensure_ascii=False))
            return output, updated

    async def _execute(self, messages, session_id, state, event_id, memory_key, returning_visitor):
        text = messages[-1]["text"].strip()
        # Plain-language record controls map onto the same program commands.
        pending_forget = state.pop("pending_forget", False)
        if text in RECORD_ON:
            text = "/memory on"
        elif text in RECORD_OFF:
            text = "/memory off"
        elif text in RECORD_DELETE:
            state["pending_forget"] = True
            return reply("要删除全部学习记录吗？删除后找不回来。", "E",
                         {"kind": "question", "text": "确认删除请回复“确定删除”，不删就继续说别的。"}), state
        elif text == "确定删除" and pending_forget:
            text = "/forget"
        record = state.get("onboarding") or {}
        if (text == "/memory on" and record.get("who") == "self"
                and record.get("stage") in onboarding.MAYBE_UNDER_14 and record.get("consent_by") != "guardian"):
            # A possibly under-14 learner cannot switch records on alone.
            state["onboarding"] = {**record, "step": "consent_guardian"}
            return reply("开启学习记录需要家长同意。", "G",
                         {"kind": "question", "text": onboarding.question_text("consent_guardian")}), state
        if text in ("/memory on", "/memory off", "/forget"):
            if text == "/forget":
                for key in list(self._accepted):
                    if key[0] == session_id:
                        del self._accepted[key]
                complete = True
                for remove, key in ((self.store.clear, memory_key), (self.artifacts.delete_session, session_id)):
                    try:
                        remove(key)
                    except (OSError, StorageError):
                        complete = False
                        LOG.warning("forget_cleanup_incomplete")
                message = ("已删除本会话保存的学习笔记和可视化文件，并关闭记忆。客户端的聊天记录需在客户端删除。" if complete else
                           "已清空当前学习任务并停止读取和记录笔记，但部分已存内容未能删除。请稍后重试 /forget。")
                after = {"memory_blocked": True}
                if record:
                    after["onboarding"] = {"step": "done", "records": "off", "stage": record.get("stage"),
                                           "who": record.get("who")}
                return reply(message, "E"), after
            enabled = text == "/memory on"
            if not enabled:
                state["memory_blocked"] = True
            try:
                self.store.set_enabled(memory_key, enabled)
            except StorageError:
                if enabled:
                    return reply("未能开启记忆，本轮不会记录笔记。请稍后重试 /memory on。", "E"), state
                return reply("本会话已暂停读取和记录笔记，但持久开关未能更新。请稍后重试 /memory off。", "E"), state
            state["memory_blocked"] = not enabled
            return reply("好，从现在开始保存学习进度。想关掉随时说“关闭记录”。" if enabled else
                         "好，不再记录学习进度。已经保存的内容想删掉，可以说“删除记录”。", "E"), state

        # Discard the visible history preceding the most recent explicit forget.
        forgotten = last_forget(messages)
        if forgotten >= 0:
            messages = messages[forgotten + 1:]

        record = state.get("onboarding")
        if self.demo_stage and record is None:
            record = state["onboarding"] = {"step": "done", "stage": self.demo_stage, "records": "on",
                                            "who": "self", "demo": True}
            try:
                self.store.set_enabled(memory_key, True)
                state["memory_blocked"] = False
            except StorageError:
                LOG.warning("demo_records_unavailable")
        elif (self.onboarding and record is None and not returning_visitor
              and not self.store.is_enabled(memory_key)):
            state["onboarding"] = onboarding.start(text)
            note = ("你的问题我先记下了，回答两个小问题后马上讲。" if state["onboarding"]["pending_question"]
                    else "开始之前，先问两个小问题，好按你的情况来讲。")
            greeting = ("你好！我是你的学习教练，可以给你讲概念、陪你解题、出小题看看是不是真的学会了，"
                        "需要的时候还能画图帮你理解。" + note)
            return reply(greeting, "G", {"kind": "question", "text": onboarding.question_text("who")}), state
        prefix = None
        if record is not None and record.get("step") != "done":
            updated = onboarding.advance(record, text)
            if updated.get("retry"):
                updated.pop("retry")
                if not updated.get("pending_question") and len(text) > 6:
                    updated["pending_question"] = text[:4000]
                    message = "这个问题我先记下了，选完这一项马上讲。"
                else:
                    message = "没认出这个选择，再选一次就好。"
                state["onboarding"] = updated
                return reply(message, "G", {"kind": "question",
                                            "text": onboarding.question_text(updated["step"])}), state
            if updated["step"] != "done":
                state["onboarding"] = updated
                return reply("好的。", "G", {"kind": "question",
                                            "text": onboarding.question_text(updated["step"])}), state
            records_on = updated.get("records") == "on"
            try:
                self.store.set_enabled(memory_key, records_on)
                if records_on and updated.get("stage") not in (None, "不想说") and updated.get("stage_answer"):
                    answer = updated["stage_answer"]
                    self.store.apply_notes(memory_key, f"{session_id}:{event_id}:stage", {
                        "schema_version": "1.0", "concepts": [], "error_records": [], "review_plan": [],
                        "profile": [{"key": "learning_stage", "value": updated["stage"],
                                     "origin": "companion_report" if updated.get("who") == "companion" else "self_report",
                                     "evidence_quote": answer}],
                    }, answer)
            except (StorageError, ValueError, OSError):
                LOG.warning("onboarding_record_setting_failed")
                records_on = False
                updated["records"] = "off"
            state["memory_blocked"] = not records_on
            pending = updated.pop("pending_question", None)
            updated.pop("stage_answer", None)
            state["onboarding"] = updated
            closing = onboarding.closing_message(updated)
            if not pending:
                return reply(closing, "G", {"kind": "question", "text": "今天想弄懂点什么？"}), state
            # Answer the question the learner arrived with, as if just asked.
            messages, prefix = [{"role": "user", "text": pending}], closing

        # An offer to compile notes lasts one turn: yes compiles, no declines,
        # anything else simply continues the lesson.
        offered = state.pop("notes_offer", False)
        if prefix is None and (text in NOTES_REQUEST or (offered and text in NOTES_YES)):
            return await self._compile_notes(messages, session_id, state, event_id, memory_key)
        if prefix is None and offered and text in NOTES_NO:
            return reply("好，这次先不整理。之后想整理，随时说“整理笔记”。", "E",
                         {"kind": "question", "text": "接下来想学什么？"}), state
        waiting = state.get("quiz") or {}
        if prefix is None and text in SKIP_QUESTION and waiting.get("awaiting") and not waiting.get("finished"):
            return await self._replace_question(messages, session_id, state, memory_key)

        output, state = await self._teach(messages, session_id, state, event_id, memory_key, returning_visitor)
        if prefix:
            output["message"] = prefix + "\n\n" + output["message"]
            validate(output, self.response_schema)
        return output, state

    async def _teach(self, messages, session_id, state, event_id, memory_key, returning_visitor):
        text = messages[-1]["text"].strip()
        budgets = self.budgets
        numbered = number_turns(messages)
        current_turn = numbered[-1]["turn"]
        context = state.setdefault("context", {})

        visible_state = deepcopy({key: value for key, value in state.items()
                                  if key not in ("last_event", "last_input", "last_response", "last_execution",
                                                 "context")})
        # Answer keys are private program state, never routing or public-response context.
        quiz = visible_state.get("quiz")
        if quiz and quiz.get("awaiting"):
            quiz["awaiting"] = public_quiz(quiz["awaiting"])
        memory_enabled = not state.get("memory_blocked", False) and self.store.is_enabled(memory_key)
        memory = self.store.snapshot(memory_key) if memory_enabled else {
            "schema_version": "1.0", "profile": {}, "concepts": [], "error_records": [], "review_plan": [],
        }
        # First meeting: the client has not seen this learner before, there is no
        # reply yet in this conversation and nothing remembered.
        first_meeting = (not returning_visitor and "onboarding" not in state
                         and not any(item["role"] == "assistant" for item in messages)
                         and not (memory.get("profile") or memory.get("concepts")))
        focus = " ".join(filter(None, (state.get("active_task"), (state.get("quiz") or {}).get("topic"), text)))
        memory_view = memory_slice(memory, focus, budgets.memory)

        # Recent turns within the budget; what falls out of it lives on in the
        # conversation notes the coordinator keeps.
        window = recent_window(numbered, budgets.decide_window, min_turns=budgets.min_turns,
                               message_max=budgets.message_max)
        decide_input = {
            "phase": "decide", "messages": window,
            "context_notes": context.get("notes") or empty_notes(),
            "learner": learner_card(state, memory, memory_enabled=memory_enabled),
            "state": visible_state, "memory": memory_view,
            "memory_enabled": memory_enabled, "first_meeting": first_meeting,
        }
        decision = validate(await self.runner("coordinator", decide_input, self.decision_schema),
                            self.decision_schema)
        context["notes"], note_problems = accept_notes(
            decision.get("context_notes"), context.get("notes"), numbered, budgets.notes)
        # Read before any routing repair: repaired decisions are rebuilt without it.
        check = decision.get("check_question")
        notes_now = context.get("notes")
        sub_window = [{"turn": item["turn"], "role": item["role"], "text": item["text"]}
                      for item in recent_window(numbered, budgets.sub_window, min_turns=1,
                                                message_max=budgets.message_max)]
        context_report = {
            "window_turns": [window[0]["turn"], current_turn] if window else [],
            "note_problems": note_problems, "decide_tokens": count_tokens(decide_input),
        }

        decision, routing_repaired = recover_quiz_route(
            decision, state, messages[-1]["text"], memory_enabled=memory_enabled,
        )
        repair_kind = "pending_quiz_answer" if routing_repaired else None
        if repair_kind is None:
            decision, practice_repaired = recover_practice_route(decision, state, messages[-1]["text"])
            if practice_repaired:
                repair_kind = "practice_without_tools"
        if repair_kind is None:
            decision, visual_repaired = recover_explicit_visual_route(decision, messages[-1]["text"])
            if visual_repaired:
                repair_kind = "explicit_visual_without_tools"
        previous_task, previous_quiz = state.get("active_task"), deepcopy(state.get("quiz"))
        if decision["mode"] == "answer":
            output = decision["response"]
            task = decision["task"]
            primary = output["intent"]["primary"]
            self._apply_task(state, primary, task)
            self._track_tasks(state, previous_task, previous_quiz, current_turn, text)
            stopping = task["end_task"] or task["pause_task"]
            self._validate_response(output, output["intent"], None, stopping=stopping)
            if stopping:
                output["next_step"] = None
            picture = None
            if check and primary in "AB" and not stopping:
                # A graded check question on what was just explained replaces the
                # coordinator's own closing question.
                picture = await self._add_check_question(
                    output, check, state, session_id, brief_card(state, memory, default_participant(state)),
                    notes_now, sub_window)
                self._validate_response(output, output["intent"], None)
            if picture:
                output["message"] += f"\n\n[{QUIZ_PICTURE_LINK}]({picture['url']})"
            validate(output, self.response_schema)
            if picture:
                self.artifacts.commit(session_id, picture)
            state["last_execution"] = {"mode": "answer", "tools": empty_results(), "context": context_report}
            if (state.get("quiz") or {}).get("kind") == "check" and check and not stopping and primary in "AB":
                state["last_execution"]["check_question"] = bool((state.get("quiz") or {}).get("awaiting"))
            return output, state

        plan, tool_results = select_tools(decision["plan"], messages[-1]["text"], memory_enabled=memory_enabled)
        primary = plan["intent"]["primary"]
        self._apply_task(state, primary, plan)
        self._track_tasks(state, previous_task, previous_quiz, current_turn, text)
        brief = brief_card(state, memory, plan["participant"])
        pictures = []
        if tool_results["notes"] == "pending":
            # Learning evidence is only marked here; notes are compiled after a
            # quiz set when the learner agrees, from the learner's own words.
            tool_results["notes"] = "deferred"
            marks = [turn for turn in context.get("evidence_turns", []) if turn != current_turn]
            context["evidence_turns"] = (marks + [current_turn])[-20:]

        # Grading is program state and needs no model; do it first so the evidence is ready.
        quiz_context, quiz_evidence, pending_quiz = self._quiz_grade(plan, text, state)
        # The coordinator declared content dependencies; the program derives the order.
        order = execution_order(plan)
        running = {}

        async def run_research():
            if not plan["research_query"]:
                return None
            try:
                found = await self.research.run(plan["research_query"], learner=brief)
            except (StageFailure, OSError, TimeoutError, ValueError):
                found = {"overall": "unavailable", "sources": [], "claims": [],
                         "queries": [], "warnings": ["research_failed"]}
            tool_results["research"] = found["overall"]
            return found

        async def prepare_visual():
            if tool_results["visual"] != "pending":
                return None, None
            # A visual of stable knowledge does not wait for, or receive, this turn's research.
            found = await running["research"] if order["visual_waits_research"] else None
            result = await VisualGenerationService(self.runner, self.artifacts, self.image_generator).generate(
                session_id, {
                    "goal": plan["visual_request"], "research": found,
                    "learning_goal": (plan["task_summary"] or "")[:400],
                    "recent_messages": sub_window,
                    "profile": memory_view.get("profile", {}),
                    "learner": brief,
                    "context_notes": note_slice(notes_now, "covered", "requests"),
                }, learner_text=text)
            tool_results["visual"] = "ready" if result[0] else "unavailable"
            return result

        async def prepare_quiz():
            if pending_quiz is None:
                return quiz_context
            extra = {"learner": brief, "context_notes": note_slice(notes_now, "covered", "stuck"),
                     "recent_messages": sub_window}
            if order["quiz_waits_research"]:
                found = research_digest(await running["research"])
                extra["research"] = {"overall": found["overall"], "claims": [
                    claim for claim in found["claims"] if claim["status"] in ("verified", "likely")]}
            if order["quiz_waits_visual"]:
                _, visual_context = await running["visual"]
                if visual_context and visual_context.get("status") == "ready":
                    extra["visual_context"] = visual_context
            question, picture = await self._quiz_question(plan["participant"], pending_quiz, extra,
                                                          session_id=session_id)
            if picture:
                pictures.append(picture)
            return question

        # Independent work runs concurrently; declared dependencies wait inside
        # their own task. Nothing writes memory or publishes artifacts until the
        # final response has passed validation.
        reservation = CALL_RESERVE.set(2)  # respond plus its allowed format retry
        try:
            async with asyncio.TaskGroup() as tasks:
                running["research"] = tasks.create_task(run_research())
                running["visual"] = tasks.create_task(prepare_visual())
                quiz_task = tasks.create_task(prepare_quiz())
        except ExceptionGroup:
            # TaskGroup first cancels/joins siblings. Keep unexpected child
            # failures behind the same fixed turn fallback, without leaking
            # private exception text. External cancellation still propagates.
            raise StageFailure("preparation_failed") from None
        finally:
            CALL_RESERVE.reset(reservation)
        research = running["research"].result()
        visual_task = running["visual"]
        quiz_context = quiz_task.result()
        if tool_results["quiz"] == "pending":
            outcome = quiz_context or {}
            tool_results["quiz"] = ("finished" if outcome.get("finished") else
                                    "question_ready" if "new_question" in outcome else
                                    "awaiting_answer" if "awaiting" in outcome else
                                    "feedback_ready" if "feedback" in outcome else "needs_input")
        artifact, visual_context = visual_task.result()
        visual_warning = tool_results["visual"] == "unavailable"
        require_finished(tool_results)

        respond_window = recent_window(numbered, budgets.respond_window, min_turns=min(2, budgets.min_turns),
                                       message_max=budgets.message_max)
        response_input = {
            "phase": "respond", "messages": respond_window,
            "context_notes": notes_now or empty_notes(),
            "learner": learner_card(state, memory, memory_enabled=memory_enabled),
            "plan": plan, "memory": memory_view,
            "state": {key: state.get(key) for key in ("active_task", "pending_tasks", "resume_stage")},
            "research": research_digest(research), "quiz": quiz_context,
            "visual_status": "ready" if artifact else "unavailable" if visual_warning else "not_requested",
            "visual_context": visual_context,
            "memory_enabled": memory_enabled, "first_meeting": first_meeting,
            "tool_results": deepcopy(tool_results),
        }
        context_report["respond_tokens"] = count_tokens(response_input)
        output = await self.runner("coordinator", response_input, self.response_schema)
        stopping = plan["end_task"] or plan["pause_task"] or plan["quiz_action"] == "stop"
        self._validate_response(output, plan["intent"], research, stopping=stopping)
        if stopping:
            output["next_step"] = None
        if quiz_context and quiz_context.get("new_question"):
            output["next_step"] = {"kind": "question", "text": render_question(quiz_context["new_question"])}
        offer_notes = False
        if quiz_context and quiz_context.get("finished"):
            finished = state.get("quiz") or {}
            results = context.get("quiz_results", [])
            results.append({"topic": finished.get("topic") or plan["quiz_topic"], "kind": finished.get("kind", "practice"),
                            "answered": quiz_context.get("answered"), "correct": quiz_context.get("correct_count"),
                            "suspected_errors": finished.get("errors", [])})
            context["quiz_results"] = results[-5:]
            if finished.get("kind") != "check":
                output["next_step"] = None
                if memory_enabled:
                    # A practice set is done: ask once whether to compile notes.
                    output["next_step"] = {"kind": "question", "text": NOTES_OFFER}
                    offer_notes = True
        if (check and not stopping and primary in "AB"
                and tool_results["quiz"] in ("not_requested", "skipped_policy")):
            picture = await self._add_check_question(output, check, state, session_id, brief, notes_now, sub_window)
            if picture:
                pictures.append(picture)

        if tool_results["quiz"] in ("needs_input", "awaiting_answer") and (
                not output["next_step"] or output["next_step"]["kind"] != "question"):
            raise ContractError("quiz_wait_requires_question")
        # Also check the program-selected question text before appending trusted
        # source/artifact links; schemas alone do not validate URLs in text.
        self._validate_response(output, plan["intent"], research, stopping=stopping)

        if research is not None:
            labels = {"unavailable": "本次未取得可核查资料，未完成检索验证。",
                      "likely": "目前证据有限，尚未完成独立来源交叉核实。",
                      "disputed": "来源存在冲突，相关说法不能当作确定结论。"}
            if research["overall"] in labels:
                output["message"] += "\n\n" + labels[research["overall"]]
            sources = research.get("sources", [])[:3]
            if sources:
                output["message"] += "\n\n参考资料：" + "；".join(
                    f"[{source['title'].replace('[', '（').replace(']', '）')}]({source['url']})" for source in sources)
        if artifact:
            output["message"] += f"\n\n[打开互动图]({artifact['url']})"
        elif visual_warning:
            output["message"] += "\n\n这次未生成可用的互动图。"
        for picture in pictures:
            output["message"] += f"\n\n[{QUIZ_PICTURE_LINK}]({picture['url']})"
        validate(output, self.response_schema)

        # Nothing persistent is committed before public output is valid.
        if artifact:
            self.artifacts.commit(session_id, artifact)
            tool_results["visual"] = "published"
        for picture in pictures:
            self.artifacts.commit(session_id, picture)
        if quiz_evidence is not None and memory_enabled:
            try:
                # Records under a learner ID span conversations, so the turn key
                # then includes the conversation to keep turns distinct.
                turn_key = event_id if memory_key == session_id else f"{session_id}:{event_id}"
                self.store.apply_notes(memory_key, turn_key, empty_learning_notes(), text,
                                       quiz_evidence=quiz_evidence)
            except (ValueError, OSError, StorageError):
                output["message"] += "\n\n本轮做题记录未保存。"
        if offer_notes:
            state["notes_offer"] = True
        require_finished(tool_results)
        state["last_execution"] = {"mode": "tools", "tools": tool_results, "order": order,
                                   "context": context_report}
        if repair_kind:
            state["last_execution"]["routing_repaired"] = repair_kind
        return output, state

    async def _compile_notes(self, messages, session_id, state, event_id, memory_key):
        """Turn this conversation's learner messages into learning notes, once agreed."""
        failed = reply("这次笔记没有整理成功，学习记录没有改动。想再试，可以说“整理笔记”。", "E",
                       {"kind": "question", "text": "接下来想学什么？"})
        memory_enabled = not state.get("memory_blocked", False) and self.store.is_enabled(memory_key)
        if not memory_enabled:
            return reply("现在没有开启学习记录，笔记不会保存。想保存，可以先说“开启记录”。", "E"), state
        context = state.setdefault("context", {})
        numbered = number_turns(messages)
        earlier = [item for item in numbered if item["turn"] < numbered[-1]["turn"]]
        shown = learner_messages(earlier, after_turn=context.get("notes_upto_turn", 0),
                                 budget=self.budgets.notes_messages, message_max=self.budgets.message_max)
        if not shown:
            return reply("这次对话里还没有新的内容可以整理。", "E",
                         {"kind": "question", "text": "接下来想学什么？"}), state
        turns = {item["turn"] for item in shown}
        # Evidence must quote what the learner actually said in these turns.
        said = "\n".join(item["text"] for item in earlier if item["role"] == "user" and item["turn"] in turns)
        memory = self.store.snapshot(memory_key)
        payload = {
            "learner_messages": shown,
            "evidence_turns": [turn for turn in context.get("evidence_turns", []) if turn in turns],
            "context_notes": note_slice(context.get("notes"), "covered", "stuck"),
            "quiz_results": context.get("quiz_results", []),
            "learner": brief_card(state, memory),
            "memory": memory_slice(memory, " ".join(item["text"] for item in shown)[-2000:], self.budgets.memory),
        }
        try:
            notes = validate(await self.runner("notes", payload, NOTES_SCHEMA), NOTES_SCHEMA)
            key = f"notes:{event_id}" if memory_key == session_id else f"{session_id}:notes:{event_id}"
            written = self.store.apply_notes(memory_key, key, notes, said)
        except (StageFailure, ContractError, ValueError, OSError, StorageError):
            LOG.warning("notes_compile_failed")
            return failed, state
        # Up to and including this request, so the request itself is never compiled later.
        context["notes_upto_turn"] = numbered[-1]["turn"]
        context["evidence_turns"] = []
        counts = [(len(notes["concepts"]), "个知识点"), (len(notes["error_records"]), "个易错点"),
                  (len(notes["review_plan"]), "项复习安排"), (len(notes["profile"]), "条学习情况")]
        saved = "、".join(f"{number} {label}" for number, label in counts if number)
        if not written:
            message = "这部分内容之前已经整理过了。"
        elif saved:
            message = f"整理好了，记下了{saved}。下次可以接着复习。"
        else:
            message = "看了一遍，这次没有需要新记下的内容。"
        return reply(message, "E", {"kind": "question", "text": "接下来想学什么？"}), state

    @staticmethod
    def _track_tasks(state, previous_task, previous_quiz, turn, text):
        """Program-kept task cards and the learner's goal in their own words."""
        context = state.setdefault("context", {})
        active = state.get("active_task")
        if active and active != previous_task:
            context["goal"] = {"turn": turn, "text": text[:300]}
        if previous_task and not active and not state.get("pending_tasks", []).count(previous_task):
            card = {"task": previous_task, "status": "已结束"}
            if previous_quiz and previous_quiz.get("answered"):
                card["quiz"] = f"{previous_quiz['answered']} 题对 {previous_quiz['correct']} 题"
            context["finished"] = (context.get("finished", []) + [card])[-5:]

    def _validate_response(self, output, intent, research, *, stopping=False):
        validate(output, self.response_schema)
        if output["intent"] != intent:
            raise ContractError("intent_changed_after_routing")
        if not stopping and intent["primary"] == "G" and (not output["next_step"] or output["next_step"]["kind"] != "question"):
            raise ContractError("clarification_requires_question")
        allowed = {source["url"] for source in (research or {}).get("sources", [])}
        for url in re.findall(r"https?://[^\s)<>]+", output["message"] + (output["next_step"] or {}).get("text", ""), re.IGNORECASE):
            if url not in allowed:
                raise ContractError("unretrieved_url")

    @staticmethod
    def _apply_task(state, primary, task):
        if task["end_task"] or task.get("quiz_action") == "stop":
            state.pop("quiz", None)
            state["active_task"] = None
            state["pending_tasks"] = []
            state.pop("resume_stage", None)
        elif primary == "E" or task["pause_task"]:
            if state.get("active_task"):
                state["resume_stage"] = "paused"
            if task["task_summary"]:
                pending = state.setdefault("pending_tasks", [])
                if task["task_summary"] not in pending:
                    pending.append(task["task_summary"])
                state["pending_tasks"] = pending[-5:]
        elif task["task_summary"]:
            state["active_task"] = task["task_summary"]
            state["resume_stage"] = "active"
            state["pending_tasks"] = [item for item in state.get("pending_tasks", [])
                                      if item != task["task_summary"]]

    def _quiz_grade(self, plan, text, state):
        """Program-owned grading and quiz state; no model call.

        Returns (context, evidence, quiz). quiz is set only when this turn still
        needs a new question, which _quiz_question generates afterwards. A wrong
        answer carries the suspected error type marked on the chosen option.
        """
        if plan["intent"]["primary"] not in "ABC" or plan["end_task"] or plan["pause_task"]:
            return None, None, None
        action = plan["quiz_action"]
        if action not in ("start", "continue"):
            return None, None, None
        quiz = state.get("quiz")
        # A check question left unanswered gives way to a practice set the learner asked for.
        replace_check = action == "start" and quiz and quiz.get("kind") == "check" and not quiz.get("finished")
        # continue without an active quiz is degraded to start: the learner
        # asked for more practice, so a missing state record must not kill the turn.
        if not quiz or quiz.get("finished") or replace_check:
            if not plan["quiz_topic"]:
                return {"needs_topic": True}, None, None
            seen = (quiz or {}).get("question_history", [])[-20:]
            quiz = {"topic": plan["quiz_topic"], "total": plan["quiz_total"],
                    "difficulty": plan["difficulty"], "asked": 0, "answered": 0,
                    "correct": 0, "awaiting": None, "finished": False, "question_history": list(seen),
                    "participant": plan["participant"], "kind": "practice", "errors": []}
            state["quiz"] = quiz
        quiz["participant"] = conservative_participant(quiz.get("participant", "unknown"), plan["participant"])
        # Grading evidence, notes, new questions and the final reply must agree
        # on authorship, including on a normal (unrepaired) continuation.
        plan["participant"] = quiz["participant"]
        evidence = None
        if quiz.get("awaiting"):
            question = quiz["awaiting"]
            selected = selected_choice(text, question)
            if selected is None:
                return {"awaiting": public_quiz(question), "needs_clear_choice": True}, None, None
            correct = selected == question["answer_index"]
            quiz["answered"] += 1
            quiz["correct"] += int(correct)
            quiz["awaiting"] = None
            evidence = {"concept_id": question["concept_id"], "correct": correct,
                        "difficulty": quiz["difficulty"], "independent": plan["participant"] == "learner",
                        "actor": {"learner": "learner", "companion_report": "companion", "unknown": "unknown"}[plan["participant"]],
                        "kind": "quiz"}
            feedback = {"correct": correct, "explanation": question["explanation"]}
            diagnosis = None if correct else next(
                (item for item in question.get("diagnoses", []) if item["choice_index"] == selected), None)
            if diagnosis:
                # Suspected, not confirmed: it comes from the option chosen. The
                # learner's own reasoning is what confirms it later.
                evidence["error_category"] = diagnosis["error_type"]
                record = {"concept_id": question["concept_id"], "error_type": diagnosis["error_type"],
                          "misconception": diagnosis["misconception"]}
                quiz.setdefault("errors", []).append(record)
                context = state.setdefault("context", {})
                history = (context.get("error_history", []) + [record])[-20:]
                context["error_history"] = history
                feedback["suspected_error"] = {"type": ERROR_LABELS[diagnosis["error_type"]],
                                               "code": diagnosis["error_type"],
                                               "misconception": diagnosis["misconception"],
                                               "basis": "所选选项对应的常见误解，尚未经学习者确认"}
                repeats = sum(item["concept_id"] == record["concept_id"] and item["error_type"] == record["error_type"]
                              for item in history)
                if repeats >= 2:
                    feedback["repeated_error"] = True
            if quiz["answered"] >= quiz["total"]:
                quiz["finished"] = True
            # Feedback occupies this turn; never advance before error diagnosis.
            if not correct or quiz["finished"]:
                return {"feedback": feedback, "answered": quiz["answered"], "correct_count": quiz["correct"],
                        "finished": quiz["finished"], "needs_review": not correct,
                        "kind": quiz.get("kind", "practice")}, evidence, None
        if quiz.get("finished"):
            return None, evidence, None
        return None, evidence, quiz

    async def _quiz_question(self, participant, quiz, context=None, *, session_id=None):
        """Ask the quiz writer for one new question.

        Returns (public question context, picture artifact or None). The program
        checks what the model cannot be trusted with: one answer in range, every
        wrong choice diagnosed exactly once, no repeated question. A requested
        picture is drawn here; if that is not possible the writer is asked once
        more for a text-only question.
        """
        seen = quiz.setdefault("question_history", [])
        known = {item.strip().casefold() for item in seen}
        request = {"phase": "quiz", "kind": quiz.get("kind", "practice"), "topic": quiz["topic"],
                   "difficulty": quiz["difficulty"], "count": 1, "participant": participant,
                   "previous_questions": list(seen), "require_new_question": True}
        request.update(context or {})
        picture, problem, rejected = None, None, 0
        # Two tries for a sound question; one more only to swap a picture that
        # could not be drawn for a text-only question.
        for _ in range(3):
            if problem:
                request["correction"] = problem
            question = validate(await self.runner("quizzer", request, QUIZ_SCHEMA), QUIZ_SCHEMA)
            problem = check_question(question)
            if problem is None and question["question"].strip().casefold() in known:
                problem = "repeated_question"
            if problem is None and question["image"] is not None:
                if request.get("image_unavailable"):
                    problem = "picture_not_available_write_text_only"
                else:
                    picture = await self._question_picture(session_id, question)
                    if picture is None:
                        request["image_unavailable"] = True
                        problem = "picture_not_available_write_text_only"
            if problem is None:
                break
            if problem != "picture_not_available_write_text_only":
                rejected += 1
                if rejected >= 2:
                    raise StageFailure("quiz_question_not_accepted")
        else:
            raise StageFailure("quiz_question_not_accepted")
        quiz["awaiting"] = question
        seen.append(question["question"])
        quiz["asked"] += 1
        public = public_quiz(question)
        public["has_picture"] = picture is not None
        return {"new_question": public, "answered": quiz["answered"], "total": quiz["total"]}, picture

    async def _question_picture(self, session_id, question):
        """Draw the picture a question asks about; None when it cannot be drawn."""
        if self.image_generator is None or session_id is None:
            return None
        spec = {"kind": "illustration", "title": "题目配图",
                "prompt": question["image"]["prompt"], "alt": question["image"]["alt"]}
        try:
            validate(spec, VISUAL_SCHEMA)
            image = await asyncio.wait_for(self.image_generator(spec["prompt"]), timeout=60)
            return self.artifacts.prepare(session_id, spec, image=image)
        except (ContractError, RuntimeError, OSError, TimeoutError, ValueError, StorageError):
            # Provider details never reach logs or the learner.
            LOG.warning("quiz_picture_unavailable")
            return None

    async def _add_check_question(self, output, check, state, session_id, brief, notes_now, sub_window):
        """One graded question on what the reply just explained. Returns its picture, if any.

        A running practice set is left alone. If no acceptable question comes
        back, the coordinator's own closing question stays.
        """
        running = state.get("quiz") or {}
        if running and not running.get("finished") and running.get("kind", "practice") != "check":
            return None
        quiz = {"topic": check["topic"], "total": 1, "difficulty": check["difficulty"], "asked": 0,
                "answered": 0, "correct": 0, "awaiting": None, "finished": False,
                "question_history": list(running.get("question_history", [])[-20:]),
                "participant": brief.get("participant") or default_participant(state), "kind": "check", "errors": []}
        extra = {"explanation": output["message"][:3000], "learner": brief,
                 "context_notes": note_slice(notes_now, "covered", "stuck"), "recent_messages": sub_window}
        try:
            question, picture = await self._quiz_question(quiz["participant"], quiz, extra, session_id=session_id)
        except (StageFailure, ContractError):
            LOG.warning("check_question_unavailable")
            return None
        state["quiz"] = quiz
        output["next_step"] = {"kind": "question", "text": render_question(question["new_question"])}
        return picture

    async def _replace_question(self, messages, session_id, state, memory_key):
        """“换一题”: drop the waiting question without grading and ask a new one."""
        before = deepcopy(state.get("quiz"))
        quiz = state["quiz"]
        quiz["awaiting"] = None
        quiz["skipped"] = quiz.get("skipped", 0) + 1
        memory_enabled = not state.get("memory_blocked", False) and self.store.is_enabled(memory_key)
        memory = self.store.snapshot(memory_key) if memory_enabled else {}
        numbered = number_turns(messages)
        extra = {"learner": brief_card(state, memory, quiz.get("participant")),
                 "context_notes": note_slice((state.get("context") or {}).get("notes"), "covered", "stuck"),
                 "recent_messages": recent_window(numbered, self.budgets.sub_window, min_turns=1,
                                                  message_max=self.budgets.message_max)}
        try:
            question, picture = await self._quiz_question(quiz.get("participant", "unknown"), quiz, extra,
                                                          session_id=session_id)
        except (StageFailure, ContractError):
            LOG.warning("replacement_question_unavailable")
            state["quiz"] = before
            return reply("这次没能换题。可以先试着回答这道，或者说“停止练习”。", "C",
                         {"kind": "question", "text": render_question(before["awaiting"])}), state
        output = reply("好，这题不算分，换一道。", "C",
                       {"kind": "question", "text": render_question(question["new_question"])})
        if picture:
            output["message"] += f"\n\n[{QUIZ_PICTURE_LINK}]({picture['url']})"
        validate(output, self.response_schema)
        if picture:
            self.artifacts.commit(session_id, picture)
        return output, state
