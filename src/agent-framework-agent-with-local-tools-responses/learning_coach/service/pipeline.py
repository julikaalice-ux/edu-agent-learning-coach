"""Explicit routing, evidence handoffs, quiz progress and commit-after-validation."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import logging
import re

from learning_coach.domain.contracts import QUIZ_SCHEMA, ContractError, decision_schema, grade_choice, public_quiz, validate
from learning_coach.domain.note_contracts import NOTES_SCHEMA
from learning_coach.domain.routing import (conservative_participant, empty_results, recover_explicit_visual_route,
                      recover_practice_route,
                      recover_quiz_route, require_finished, select_tools)
from learning_coach.service.execution import BUDGET, CALL_RESERVE, CallBudget, StageFailure
from learning_coach.service.visual_generation import VisualGenerationService
from learning_coach.service.ports import AgentRunner, ResearchPort, LearningMemory, Artifacts, ImageGenerator, StorageError


LOG = logging.getLogger(__name__)


def reply(message, primary="G", step=None):
    return {"schema_version": "1.0", "intent": {"primary": primary, "is_implicit": False},
            "message": message, "next_step": step}


class LearningPipeline:
    def __init__(self, runner: AgentRunner, research: ResearchPort, store: LearningMemory,
                 artifacts: Artifacts, response_schema: dict, *,
                 image_generator: ImageGenerator | None = None):
        self.runner, self.research = runner, research
        self.store, self.artifacts = store, artifacts
        self.response_schema = response_schema
        # Dependency inversion: the pipeline depends only on this callable
        # abstraction, never on a concrete image backend. Composition roots
        # (server.py / tests) inject pollinations, FLUX, or a fake.
        self.image_generator = image_generator
        self.decision_schema = decision_schema(response_schema)
        self._locks = {}
        self._recent = OrderedDict()
        self._accepted = OrderedDict()

    async def run(self, messages, *, session_id, state=None, turn_id=None):
        # IDs are supplied by the host/application, never by model output.
        if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
            raise ValueError("invalid_session_id")
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
                    self._execute(clean, session_id, working, event_id), timeout=180,
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

    async def _execute(self, messages, session_id, state, event_id):
        text = messages[-1]["text"].strip()
        if text in ("/memory on", "/memory off", "/forget"):
            if text == "/forget":
                for key in list(self._accepted):
                    if key[0] == session_id:
                        del self._accepted[key]
                complete = True
                for remove in (self.store.clear, self.artifacts.delete_session):
                    try:
                        remove(session_id)
                    except (OSError, StorageError):
                        complete = False
                        LOG.warning("forget_cleanup_incomplete")
                message = ("已删除本会话保存的学习笔记和可视化文件，并关闭记忆。客户端的聊天记录需在客户端删除。" if complete else
                           "已清空当前学习任务并停止读取和记录笔记，但部分已存内容未能删除。请稍后重试 /forget。")
                return reply(message, "E"), {"memory_blocked": True}
            enabled = text == "/memory on"
            if not enabled:
                state["memory_blocked"] = True
            try:
                self.store.set_enabled(session_id, enabled)
            except StorageError:
                if enabled:
                    return reply("未能开启记忆，本轮不会记录笔记。请稍后重试 /memory on。", "E"), state
                return reply("本会话已暂停读取和记录笔记，但持久开关未能更新。请稍后重试 /memory off。", "E"), state
            state["memory_blocked"] = not enabled
            return reply("已开启本会话的学习笔记记录；可用 /memory off 暂停，/forget 删除。" if enabled else
                         "已停止记录和读取学习笔记；已有笔记可用 /forget 删除。", "E"), state

        # Discard the visible history preceding the most recent explicit forget.
        forgotten_at = max((i for i, item in enumerate(messages)
                            if item["role"] == "user" and item["text"].strip() == "/forget"), default=-1)
        if forgotten_at >= 0:
            messages = messages[forgotten_at + 1:]

        visible_state = deepcopy({key: value for key, value in state.items()
                                  if key not in ("last_event", "last_input", "last_response", "last_execution")})
        # Answer keys are private program state, never routing or public-response context.
        quiz = visible_state.get("quiz")
        if quiz and quiz.get("awaiting"):
            quiz["awaiting"] = public_quiz(quiz["awaiting"])
        memory_enabled = not state.get("memory_blocked", False) and self.store.is_enabled(session_id)
        memory = self.store.snapshot(session_id) if memory_enabled else {
            "schema_version": "1.0", "profile": {}, "concepts": [], "error_records": [], "review_plan": [],
        }
        decision = validate(await self.runner("coordinator", {
            "phase": "decide", "messages": messages, "state": visible_state, "memory": memory,
            "memory_enabled": memory_enabled,
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

        research = None
        if plan["research_query"] and not plan["end_task"]:
            try:
                research = await self.research.run(plan["research_query"])
            except (StageFailure, OSError, TimeoutError, ValueError):
                research = {"overall": "unavailable", "sources": [], "claims": [],
                            "queries": [], "warnings": ["research_failed"]}
            tool_results["research"] = research["overall"]

        quiz_context, quiz_evidence = await self._quiz(plan, text, state)
        if tool_results["quiz"] == "pending":
            context = quiz_context or {}
            tool_results["quiz"] = ("finished" if context.get("finished") else
                                    "question_ready" if "new_question" in context else
                                    "awaiting_answer" if "awaiting" in context else
                                    "feedback_ready" if "feedback" in context else "needs_input")

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
            result = await VisualGenerationService(self.runner, self.artifacts, self.image_generator).generate(
                session_id, {
                    "goal": plan["visual_request"], "research": research,
                    "learning_goal": (plan["task_summary"] or "")[:400],
                    "recent_messages": [{"role": item["role"], "text": item["text"][:1500]}
                                        for item in messages[-4:]],
                    "profile": memory.get("profile", {}),
                }, learner_text=text)
            tool_results["visual"] = "ready" if result[0] else "unavailable"
            return result

        # Independent preparation runs concurrently; nothing writes memory or
        # publishes artifacts until the final response has passed validation.
        reservation = CALL_RESERVE.set(2)  # respond plus its allowed format retry
        try:
            async with asyncio.TaskGroup() as tasks:
                notes_task = tasks.create_task(prepare_notes())
                visual_task = tasks.create_task(prepare_visual())
        except ExceptionGroup:
            # TaskGroup first cancels/joins siblings. Keep unexpected child
            # failures behind the same fixed turn fallback, without leaking
            # private exception text. External cancellation still propagates.
            raise StageFailure("preparation_failed") from None
        finally:
            CALL_RESERVE.reset(reservation)
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
            "memory_enabled": memory_enabled,
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
                written = self.store.apply_notes(session_id, event_id, notes, text, quiz_evidence=quiz_evidence)
                tool_results["notes"] = "saved" if written else "not_written"
                if not written:
                    output["message"] += "\n\n本轮未新增学习笔记。"
            except (ValueError, OSError, StorageError):
                output["message"] += "\n\n本轮学习笔记未保存。"
                tool_results["notes"] = "failed"
        require_finished(tool_results)
        state["last_execution"] = {"mode": "tools", "tools": tool_results}
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

    async def _quiz(self, plan, text, state):
        if plan["intent"]["primary"] not in "ABC" or plan["end_task"] or plan["pause_task"]:
            return None, None
        action = plan["quiz_action"]
        if action not in ("start", "continue"):
            return None, None
        quiz = state.get("quiz")
        # continue without an active quiz is degraded to start: the learner
        # asked for more practice, so a missing state record must not kill the turn.
        if action in ("start", "continue") and (not quiz or quiz.get("finished")):
            if not plan["quiz_topic"]:
                return {"needs_topic": True}, None
            quiz = {"topic": plan["quiz_topic"], "total": plan["quiz_total"],
                    "difficulty": plan["difficulty"], "asked": 0, "answered": 0,
                    "correct": 0, "awaiting": None, "finished": False, "question_history": [],
                    "participant": plan["participant"]}
            state["quiz"] = quiz
        if not quiz or quiz.get("finished"):
            return None, None
        quiz["participant"] = conservative_participant(quiz.get("participant", "unknown"), plan["participant"])
        # Grading evidence, notes, new questions and the final reply must agree
        # on authorship, including on a normal (unrepaired) continuation.
        plan["participant"] = quiz["participant"]
        evidence = None
        if quiz.get("awaiting"):
            question = quiz["awaiting"]
            correct = grade_choice(text, question)
            if correct is None:
                return {"awaiting": public_quiz(question), "needs_clear_choice": True}, None
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
                        "finished": quiz["finished"], "needs_review": not correct}, evidence
        seen = quiz.setdefault("question_history", [])
        for attempt in range(2):
            question = validate(await self.runner("coordinator", {
                "phase": "quiz", "topic": quiz["topic"], "difficulty": quiz["difficulty"],
                "count": 1, "participant": plan["participant"], "previous_questions": seen,
                "require_new_question": True,
            }, QUIZ_SCHEMA), QUIZ_SCHEMA)
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
                "total": quiz["total"]}, evidence
