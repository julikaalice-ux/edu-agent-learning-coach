"""Explicit routing, evidence handoffs, quiz progress and commit-after-validation."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import logging
import re

from learning_coach.domain.contracts import QUIZ_SCHEMA, ContractError, decision_schema, grade_choice, public_quiz, validate
from learning_coach.domain import onboarding
from learning_coach.domain.note_contracts import NOTES_SCHEMA
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
                 onboarding: bool = False, demo_stage: str | None = None):
        self.runner, self.research = runner, research
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
        clean = [{"role": m["role"], "text": m["text"][:12000]} for m in messages[-24:]
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

        output, state = await self._teach(messages, session_id, state, event_id, memory_key, returning_visitor)
        if prefix:
            output["message"] = prefix + "\n\n" + output["message"]
            validate(output, self.response_schema)
        return output, state

    async def _teach(self, messages, session_id, state, event_id, memory_key, returning_visitor):
        text = messages[-1]["text"].strip()

        visible_state = deepcopy({key: value for key, value in state.items()
                                  if key not in ("last_event", "last_input", "last_response", "last_execution")})
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
        decision = validate(await self.runner("coordinator", {
            "phase": "decide", "messages": messages, "state": visible_state, "memory": memory,
            "memory_enabled": memory_enabled, "first_meeting": first_meeting,
        }, self.decision_schema), self.decision_schema)
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
        if decision["mode"] == "answer":
            output = decision["response"]
            task = decision["task"]
            primary = output["intent"]["primary"]
            self._apply_task(state, primary, task)
            self._validate_response(output, output["intent"], None,
                                    stopping=task["end_task"] or task["pause_task"])
            if task["end_task"] or task["pause_task"]:
                output["next_step"] = None
            validate(output, self.response_schema)
            state["last_execution"] = {"mode": "answer", "tools": empty_results()}
            return output, state

        plan, tool_results = select_tools(decision["plan"], messages[-1]["text"], memory_enabled=memory_enabled)
        primary = plan["intent"]["primary"]
        self._apply_task(state, primary, plan)

        # Grading is program state and needs no model; do it first so notes get the evidence.
        quiz_context, quiz_evidence, pending_quiz = self._quiz_grade(plan, text, state)
        # The coordinator declared content dependencies; the program derives the order.
        order = execution_order(plan)
        running = {}

        async def run_research():
            if not plan["research_query"]:
                return None
            try:
                found = await self.research.run(plan["research_query"])
            except (StageFailure, OSError, TimeoutError, ValueError):
                found = {"overall": "unavailable", "sources": [], "claims": [],
                         "queries": [], "warnings": ["research_failed"]}
            tool_results["research"] = found["overall"]
            return found

        async def prepare_notes():
            if tool_results["notes"] != "pending":
                return None
            try:
                result = validate(await self.runner("notes", {
                    "learner_text": text, "memory": memory, "quiz_evidence": quiz_evidence,
                    "participant": plan["participant"],
                }, NOTES_SCHEMA), NOTES_SCHEMA)
                tool_results["notes"] = "prepared"
                return result
            except (StageFailure, ContractError):
                tool_results["notes"] = "failed"
                return None

        async def prepare_visual():
            if tool_results["visual"] != "pending":
                return None, None
            # A visual of stable knowledge does not wait for, or receive, this turn's research.
            found = await running["research"] if order["visual_waits_research"] else None
            result = await VisualGenerationService(self.runner, self.artifacts, self.image_generator).generate(
                session_id, {
                    "goal": plan["visual_request"], "research": found,
                    "learning_goal": (plan["task_summary"] or "")[:400],
                    "recent_messages": [{"role": item["role"], "text": item["text"][:1500]}
                                        for item in messages[-4:]],
                    "profile": memory.get("profile", {}),
                }, learner_text=text)
            tool_results["visual"] = "ready" if result[0] else "unavailable"
            return result

        async def prepare_quiz():
            if pending_quiz is None:
                return quiz_context
            context = {}
            if order["quiz_waits_research"]:
                found = await running["research"]
                context["research"] = {"overall": found["overall"], "claims": [
                    claim for claim in found["claims"] if claim["status"] in ("verified", "likely")]}
            if order["quiz_waits_visual"]:
                _, visual_context = await running["visual"]
                if visual_context and visual_context.get("status") == "ready":
                    context["visual_context"] = visual_context
            return await self._quiz_question(plan, pending_quiz, context)

        # Independent work runs concurrently; declared dependencies wait inside
        # their own task. Nothing writes memory or publishes artifacts until the
        # final response has passed validation.
        reservation = CALL_RESERVE.set(2)  # respond plus its allowed format retry
        try:
            async with asyncio.TaskGroup() as tasks:
                running["research"] = tasks.create_task(run_research())
                notes_task = tasks.create_task(prepare_notes())
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
            context = quiz_context or {}
            tool_results["quiz"] = ("finished" if context.get("finished") else
                                    "question_ready" if "new_question" in context else
                                    "awaiting_answer" if "awaiting" in context else
                                    "feedback_ready" if "feedback" in context else "needs_input")
        notes = notes_task.result()
        artifact, visual_context = visual_task.result()
        notes_warning = tool_results["notes"] == "failed"
        visual_warning = tool_results["visual"] == "unavailable"
        require_finished(tool_results)

        response_input = {
            "phase": "respond", "messages": messages, "plan": plan,
            "memory": memory, "state": {key: state.get(key) for key in ("active_task", "pending_tasks", "resume_stage")},
            "research": research, "quiz": quiz_context,
            "visual_status": "ready" if artifact else "unavailable" if visual_warning else "not_requested",
            "visual_context": visual_context,
            "memory_enabled": memory_enabled, "first_meeting": first_meeting,
            "tool_results": deepcopy(tool_results),
        }
        output = await self.runner("coordinator", response_input, self.response_schema)
        stopping = plan["end_task"] or plan["pause_task"] or plan["quiz_action"] == "stop"
        self._validate_response(output, plan["intent"], research, stopping=stopping)
        if stopping:
            output["next_step"] = None
        if quiz_context and quiz_context.get("new_question"):
            q = quiz_context["new_question"]
            choices = "\n".join(f"{'ABCD'[i]}. {choice}" for i, choice in enumerate(q["choices"]))
            # The program renders exactly one unanswered question, with no answer key.
            output["next_step"] = {"kind": "question", "text": q["question"] + "\n" + choices}
        if quiz_context and quiz_context.get("finished"):
            output["next_step"] = None

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
        if notes_warning:
            output["message"] += "\n\n本轮学习笔记未保存。"
        validate(output, self.response_schema)

        # Nothing persistent is committed before public output is valid.
        if artifact:
            self.artifacts.commit(session_id, artifact)
            tool_results["visual"] = "published"
        if notes is not None:
            try:
                # Records under a learner ID span conversations, so the turn key
                # then includes the conversation to keep turns distinct.
                turn_key = event_id if memory_key == session_id else f"{session_id}:{event_id}"
                written = self.store.apply_notes(memory_key, turn_key, notes, text,
                                                 quiz_evidence=quiz_evidence)
                tool_results["notes"] = "saved" if written else "not_written"
                if not written:
                    output["message"] += "\n\n本轮未新增学习笔记。"
            except (ValueError, OSError, StorageError):
                output["message"] += "\n\n本轮学习笔记未保存。"
                tool_results["notes"] = "failed"
        require_finished(tool_results)
        state["last_execution"] = {"mode": "tools", "tools": tool_results, "order": order}
        if repair_kind:
            state["last_execution"]["routing_repaired"] = repair_kind
        return output, state

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
        needs a new question, which _quiz_question generates afterwards.
        """
        if plan["intent"]["primary"] not in "ABC" or plan["end_task"] or plan["pause_task"]:
            return None, None, None
        action = plan["quiz_action"]
        if action not in ("start", "continue"):
            return None, None, None
        quiz = state.get("quiz")
        # continue without an active quiz is degraded to start: the learner
        # asked for more practice, so a missing state record must not kill the turn.
        if action in ("start", "continue") and (not quiz or quiz.get("finished")):
            if not plan["quiz_topic"]:
                return {"needs_topic": True}, None, None
            quiz = {"topic": plan["quiz_topic"], "total": plan["quiz_total"],
                    "difficulty": plan["difficulty"], "asked": 0, "answered": 0,
                    "correct": 0, "awaiting": None, "finished": False, "question_history": [],
                    "participant": plan["participant"]}
            state["quiz"] = quiz
        if not quiz or quiz.get("finished"):
            return None, None, None
        quiz["participant"] = conservative_participant(quiz.get("participant", "unknown"), plan["participant"])
        # Grading evidence, notes, new questions and the final reply must agree
        # on authorship, including on a normal (unrepaired) continuation.
        plan["participant"] = quiz["participant"]
        evidence = None
        if quiz.get("awaiting"):
            question = quiz["awaiting"]
            correct = grade_choice(text, question)
            if correct is None:
                return {"awaiting": public_quiz(question), "needs_clear_choice": True}, None, None
            quiz["answered"] += 1
            quiz["correct"] += int(correct)
            quiz["awaiting"] = None
            evidence = {"concept_id": question["concept_id"], "correct": correct,
                        "difficulty": quiz["difficulty"], "independent": plan["participant"] == "learner",
                        "actor": {"learner": "learner", "companion_report": "companion", "unknown": "unknown"}[plan["participant"]],
                        "kind": "quiz"}
            if quiz["answered"] >= quiz["total"]:
                quiz["finished"] = True
            # Feedback occupies this turn; never advance before error diagnosis.
            if not correct or quiz["finished"]:
                return {"feedback": {"correct": correct, "explanation": question["explanation"]},
                        "answered": quiz["answered"], "correct_count": quiz["correct"],
                        "finished": quiz["finished"], "needs_review": not correct}, evidence, None
        return None, evidence, quiz

    async def _quiz_question(self, plan, quiz, context=None):
        """Generate one new question; context carries only declared dependencies."""
        seen = quiz.setdefault("question_history", [])
        for attempt in range(2):
            request = {
                "phase": "quiz", "topic": quiz["topic"], "difficulty": quiz["difficulty"],
                "count": 1, "participant": plan["participant"], "previous_questions": seen,
                "require_new_question": True,
            }
            request.update(context or {})
            question = validate(await self.runner("coordinator", request, QUIZ_SCHEMA), QUIZ_SCHEMA)
            if question["answer_index"] >= len(question["choices"]):
                raise ContractError("answer_index_out_of_range")
            if question["question"].strip().casefold() not in {item.strip().casefold() for item in seen}:
                break
        else:
            raise StageFailure("repeated_quiz_question")
        quiz["awaiting"] = question
        seen.append(question["question"])
        quiz["asked"] += 1
        return {"new_question": public_quiz(question), "answered": quiz["answered"],
                "total": quiz["total"]}
