"""Single-agent baseline for the single vs multi-agent comparison.

One model does every model job of a turn in one tool-using conversation: it
writes the search queries, judges the sources, designs the visual, writes the
question and writes the reply. Everything else is the same as the
multi-agent pipeline, so the comparison isolates the agent split:

- the same teaching rules (the prompt is assembled from the same files),
- the same model deployment and the same search, rendering and image tools,
- the same program checks and state machine: onboarding, record commands,
  grading a clear choice, quiz progress, notes consent, source-link and URL
  checks, and commit only after the reply passes validation.

What it does not have: a separate quiz writer, an independent reviewer for
visuals, and the coordinator's conversation notes (it sees the same recent
window only).
"""

from copy import deepcopy
import asyncio
import logging

from learning_coach.domain.contracts import (QUIZ_SCHEMA, TASK_SCHEMA, TEXT, DIFFICULTY, ContractError,
                                             check_question, object_schema, public_quiz, selected_choice,
                                             validate)
from learning_coach.domain.context import (learner_card, memory_slice, number_turns, recent_window)
from learning_coach.domain.note_contracts import NOTES_SCHEMA
from learning_coach.domain.research_contracts import (MAX_QUERIES, MAX_QUERY_CHARS, MAX_SOURCES,
                                                      VERIFY_SCHEMA)
from learning_coach.domain.routing import INTENT_PRACTICE
from learning_coach.domain.teaching_visuals import visual_handoff
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.domain.visual_quality import gallery_problem, needs_explanatory_visual
from learning_coach.service.execution import EXTERNAL_DECLINED, external_allowed
from learning_coach.service.pipeline import (NOTES_OFFER, PICTURE_TIMEOUT_SECONDS, QUIZ_PICTURE_LINK,
                                             LearningPipeline, default_participant, empty_learning_notes,
                                             render_question)
from learning_coach.service.ports import (Artifacts, ImageGenerator, LearningMemory, SearchProvider,
                                          SingleAgentPort, StorageError, ToolSpec)
from learning_coach.service.research import assess_claims, clean_queries, search_sources
from learning_coach.service.visual_generation import _RENDER_CORRECTIONS, find_pictures, photo_entry


LOG = logging.getLogger(__name__)

MAX_SEARCHES = 2          # search calls per turn
MAX_VISUAL_DRAFTS = 2     # same as the multi-agent draft + one redraw
MAX_QUESTION_REJECTS = 2  # same as the quiz writer's two tries

QUERIES = {"type": "array", "minItems": 1, "maxItems": MAX_QUERIES,
           "items": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS, "pattern": r"\S"}}
SEARCH_PARAMETERS = object_schema({"question": TEXT, "queries": QUERIES})
CLAIMS_PARAMETERS = object_schema({"claims": VERIFY_SCHEMA["properties"]["claims"]})
VISUAL_PARAMETERS = object_schema({"goal": TEXT, "spec": VISUAL_SCHEMA})
QUESTION_PARAMETERS = object_schema({
    "kind": {"enum": ["practice", "check"]},
    "topic": TEXT,
    "difficulty": DIFFICULTY,
    "total": {"type": "integer", "minimum": 1, "maximum": 20},
    "question": QUIZ_SCHEMA,
})


def single_turn_schema(response_schema):
    """The four-field reply plus the task controls the program applies."""
    return object_schema({
        "response": response_schema,
        "task": TASK_SCHEMA,
        "stop_quiz": {"type": "boolean"},
        "observe_learning": {"type": "boolean"},
    })


def _problem(code, message):
    return {"ok": False, "problem": code, "message": message}


class _Turn:
    """What the tools did during one turn; read by the program afterwards."""

    def __init__(self):
        self.calls = []
        self.searches = 0
        self.question_text = None
        self.queries, self.sources, self.warnings = [], {}, []
        self.claims, self.overall = None, None
        self.visual_drafts, self.artifact, self.visual_context = 0, None, None
        self.question, self.picture, self.rejects = None, None, 0
        self.denied_capabilities = set()

    def research(self):
        """Same shape as ResearchService.run(), or None when nothing was searched."""
        if not self.searches:
            return None
        warnings = list(self.warnings)
        if not self.sources:
            warnings.append("no_evidence")
        elif self.claims is None:
            warnings.append("claims_not_checked")
        return {"queries": self.queries, "sources": list(self.sources.values())[:MAX_SOURCES],
                "claims": self.claims or [], "overall": self.overall or "unavailable",
                "warnings": list(dict.fromkeys(warnings))}


class SingleAgentPipeline(LearningPipeline):
    def __init__(self, agent: SingleAgentPort, provider: SearchProvider, store: LearningMemory,
                 artifacts: Artifacts, response_schema: dict, *,
                 image_generator: ImageGenerator | None = None, image_finder=None, onboarding: bool = False,
                 demo_stage: str | None = None, budgets=None):
        super().__init__(None, None, store, artifacts, response_schema, image_generator=image_generator,
                         image_finder=image_finder, onboarding=onboarding, demo_stage=demo_stage,
                         budgets=budgets)
        self.agent, self.provider = agent, provider
        self.turn_schema = single_turn_schema(response_schema)

    async def run_stream(self, messages, *, session_id, state=None, turn_id=None,
                         learner_id=None, returning_visitor=False, external_permissions=None):
        """Run one turn while streaming the reply's message text.

        Yields {"delta": text} events as the final message is written, then a
        final {"output": ..., "state": ...} event carrying the validated reply
        (the same values a plain run() would return). The deltas are the
        unvalidated draft; the final event is authoritative.
        """
        queue = asyncio.Queue()

        def on_delta(text):
            queue.put_nowait(text)

        task = asyncio.create_task(self.run(
            messages, session_id=session_id, state=state, turn_id=turn_id,
            learner_id=learner_id, returning_visitor=returning_visitor, on_delta=on_delta,
            external_permissions=external_permissions))
        getter = asyncio.create_task(queue.get())
        try:
            while True:
                finished, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                # Drain a delta that completed together with the final task.
                if getter in finished:
                    yield {"delta": getter.result()}
                    getter = asyncio.create_task(queue.get())
                    continue
                if task in finished:
                    break
            while True:
                try:
                    yield {"delta": queue.get_nowait()}
                except asyncio.QueueEmpty:
                    break
            output, state = task.result()
            yield {"output": output, "state": state}
        finally:
            # Closing a chat stream cancels its workers, including a suspended
            # model/tool call; no abandoned turn may commit a later result.
            for worker in (getter, task):
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(getter, task, return_exceptions=True)

    # Program-triggered drafts (“换一题”, “整理笔记”) go to the same single agent.
    async def _draft_notes(self, payload):
        return await self.agent("compile_notes", payload, NOTES_SCHEMA, [])

    async def _draft_question(self, request):
        return await self.agent("write_question", request, QUIZ_SCHEMA, [])

    def _grade_waiting(self, text, state):
        """Grade a clear choice on the waiting question, exactly as the multi-agent pipeline does."""
        quiz = state.get("quiz") or {}
        question = quiz.get("awaiting")
        if not question or quiz.get("finished"):
            return None, None
        selected = selected_choice(text, question)
        if selected is None:
            return None, None
        plan = {"intent": {"primary": INTENT_PRACTICE, "is_implicit": True}, "end_task": False,
                "pause_task": False, "quiz_action": "continue", "quiz_topic": quiz.get("topic"),
                "quiz_total": quiz.get("total", 1), "difficulty": quiz.get("difficulty", "简单"),
                "participant": quiz.get("participant", "unknown")}
        graded, evidence, pending = self._quiz_grade(plan, text, state)
        quiz = state["quiz"]
        feedback = (graded or {}).get("feedback") or {
            "correct": selected == question["answer_index"], "explanation": question["explanation"]}
        info = {"feedback": feedback, "answered": quiz["answered"], "total": quiz["total"],
                "correct_count": quiz["correct"], "finished": quiz.get("finished", False),
                "kind": quiz.get("kind", "practice")}
        if pending is not None:
            info["next_question_needed"] = {"topic": quiz["topic"], "difficulty": quiz["difficulty"],
                                            "number": quiz["answered"] + 1}
        return info, evidence

    def _tools(self, turn, session_id, state, learner_text):
        def needs_permission(capability, action):
            turn.denied_capabilities.add(capability)
            if EXTERNAL_DECLINED.get():
                return _problem("permission_declined", f"本次未授权{action}。继续用已有知识和本地图解说明，不再询问授权。")
            return {**_problem("permission_required", f"{action}需要学习者明确授权。请说明用途并征求本次授权，或继续使用已有知识和本地图解。"),
                    "required_capability": capability}

        async def search(question, queries):
            turn.calls.append("search")
            if not external_allowed("research"):
                return needs_permission("research", "联网检索")
            if turn.searches >= MAX_SEARCHES:
                return _problem("search_limit", "本轮已经检索过两次，用已有资料继续。")
            turn.searches += 1
            turn.question_text = question
            cleaned, warnings = clean_queries(question, queries)
            found, more = await search_sources(self.provider, cleaned)
            turn.queries += [query for query in cleaned if query not in turn.queries]
            turn.warnings += warnings + more
            new = {key: value for key, value in found.items() if key not in turn.sources}
            turn.sources.update(new)
            if not turn.sources:
                return {"ok": True, "sources": [], "message": "没有搜到资料；不能声称已查证。"}
            return {"ok": True, "sources": list(new.values()),
                    "message": "先用 record_claims 核对要用的说法；链接由程序附在回复后面。"}

        async def record_claims(claims):
            turn.calls.append("record_claims")
            if not turn.sources:
                return _problem("no_sources", "还没有搜到资料，不能核对。")
            turn.claims, turn.overall, warnings = assess_claims(turn.sources, claims)
            turn.warnings += warnings
            return {"ok": True, "overall": turn.overall,
                    "claims": [{"text": claim["text"], "status": claim["status"]} for claim in turn.claims],
                    "message": "回复里只能把 verified 和 likely 的说法当作有依据。"}

        async def draw_visual(goal, spec):
            turn.calls.append("draw_visual")
            if turn.artifact is not None:
                return _problem("one_visual_per_turn", "本轮已经有一张图。")
            if turn.visual_drafts >= MAX_VISUAL_DRAFTS:
                return _problem("draft_limit", "本轮画图已试过两次，不再画。")
            turn.visual_drafts += 1
            try:
                validate(spec, VISUAL_SCHEMA)
            except ContractError:
                return _problem("schema_invalid", "草稿未符合图示协议；请使用给定类型与字段，不输出网页代码或额外字段。")
            explanatory = needs_explanatory_visual(goal, learner_text)
            if spec["kind"] == "illustration" and explanatory:
                return _problem("interaction_problem", "这个请求需要解释机制或演示变化；静态 AI 配图不能完成，请改用可检查的图解或分步 scene。")
            if spec["kind"] == "gallery":
                return await draw_gallery(spec, explanatory)
            try:
                image = None
                if spec["kind"] == "illustration":
                    if not external_allowed("image_generation"):
                        return needs_permission("image_generation", "调用外部图片生成服务")
                    if self.image_generator is None:
                        raise RuntimeError("image_backend_unavailable")
                    image = await asyncio.wait_for(self.image_generator(spec["prompt"]),
                                                   timeout=PICTURE_TIMEOUT_SECONDS)
                artifact = (self.artifacts.prepare(session_id, spec, image=image) if image is not None
                            else self.artifacts.prepare(session_id, spec))
            except ValueError as error:
                if str(error) == "invalid_visual_dependency":
                    return _problem("render_unavailable", "本地图形渲染不可用，图不发布。")
                return _problem("render_invalid", _RENDER_CORRECTIONS.get(
                    str(error), "草稿无法渲染；检查图形边界、元素标识、连线引用、坐标范围和数值条件。"))
            except (RuntimeError, OSError, StorageError, TimeoutError):
                return _problem("render_unavailable", "本次图示渲染或图片生成服务不可用。")
            context = visual_handoff(spec)
            context.update(status="ready", quality={"verdict": "not_reviewed", "review_scope": "none",
                                                    "pixel_reviewed": False, "attempts": turn.visual_drafts})
            context["limitations"].append("图示只经过本地渲染检查，没有独立审阅。")
            turn.artifact, turn.visual_context = artifact, context
            return {"ok": True, "visual_context": context, "message": "图的链接由程序附在回复后面，不要自己写链接。"}

        async def draw_gallery(spec, explanatory):
            # No separate reviewer in this version: each panel takes its first
            # (best-ranked, Commons first) candidate.
            if self.image_finder is None:
                return _problem("gallery_unavailable", "这个环境不能用网络图片，请改用程序画的图。")
            if not external_allowed("image_search"):
                return needs_permission("image_search", "联网搜图")
            problem = gallery_problem(spec, explanatory)
            if problem:
                return _problem(problem[0]["code"], problem[0]["message"])
            found = await find_pictures(self.image_finder, spec)
            photos = [photo_entry(options[0]) if options else None for options in found]
            if not any(photos):
                return _problem("no_pictures", "没有找到可用的网络图片；请改用程序画的图。")
            try:
                artifact = self.artifacts.prepare(session_id, spec, photos=photos)
            except ValueError as error:
                return _problem("render_invalid", "配套图（companion）" + _RENDER_CORRECTIONS.get(
                    str(error), "无法渲染；检查图形边界、元素标识、连线引用、坐标范围和数值条件。"))
            except (RuntimeError, OSError, StorageError):
                return _problem("render_unavailable", "本次图示渲染不可用。")
            context = visual_handoff(spec)
            context.update(status="ready", quality={"verdict": "not_reviewed", "review_scope": "none",
                                                    "pixel_reviewed": False, "attempts": turn.visual_drafts})
            context["limitations"].append("图片按搜索排序自动选取，没有独立审阅。")
            turn.artifact, turn.visual_context = artifact, context
            return {"ok": True, "visual_context": context, "message": "图的链接由程序附在回复后面，不要自己写链接。"}

        async def ask_question(kind, topic, difficulty, total, question):
            turn.calls.append("ask_question")
            if turn.question is not None:
                return _problem("one_question_per_turn", "本轮已经出过一道题。")
            if turn.rejects >= MAX_QUESTION_REJECTS:
                return _problem("question_limit", "本轮出题已被退回两次，不再出题。")
            try:
                validate(question, QUIZ_SCHEMA)
            except ContractError:
                turn.rejects += 1
                return _problem("schema_invalid", "题目不符合出题协议。")
            running = state.get("quiz") or {}
            active = running and not running.get("finished")
            if active and running.get("awaiting"):
                return _problem("question_waiting", "上一题还在等学习者回答，不能再出新题。")
            if kind == "check" and active and running.get("kind", "practice") != "check":
                return _problem("practice_running", "正在做一组练习，不插入检查题。")
            continue_set = kind == "practice" and active and running.get("kind", "practice") == "practice"
            history = running.get("question_history", [])[-20:]
            problem = check_question(question)
            if problem is None and question["question"].strip().casefold() in {
                    item.strip().casefold() for item in history}:
                problem = "repeated_question"
            picture = None
            if problem is None and question["image"] is not None:
                picture = await self._question_picture(session_id, question)
                if picture is None:
                    return _problem("picture_not_available_write_text_only", "配图画不出来，请改出不用图的题。")
            if problem is not None:
                turn.rejects += 1
                return _problem(problem, "题目没通过程序检查，请按问题修改后重出。")
            if not continue_set:
                participant = running.get("participant") or default_participant(state)
                running = {"topic": topic, "total": 1 if kind == "check" else total, "difficulty": difficulty,
                           "asked": 0, "answered": 0, "correct": 0, "awaiting": None, "finished": False,
                           "question_history": list(history), "participant": participant,
                           "kind": kind, "errors": []}
                state["quiz"] = running
            running["awaiting"] = question
            running.setdefault("question_history", []).append(question["question"])
            running["asked"] = running.get("asked", 0) + 1
            turn.question, turn.picture = question, picture
            return {"ok": True, "shown_to_learner": render_question(public_quiz(question)),
                    "answered": running["answered"], "total": running["total"],
                    "message": "题目由程序放在 next_step；message 里不要重复题目和选项，也不要透露答案。"}

        return [
            ToolSpec("search", "用 2–4 条搜索词检索资料，返回来源编号、标题、发布方和原文节选。", SEARCH_PARAMETERS, search),
            ToolSpec("record_claims", "核对要在回复里使用的说法：每条逐字引用节选作证据，程序检查后给出可信等级。",
                     CLAIMS_PARAMETERS, record_claims),
            ToolSpec("draw_visual", "画一张教学图（声明式规格，程序渲染），每轮最多一张。", VISUAL_PARAMETERS, draw_visual),
            ToolSpec("ask_question", "出一道单选题（练习题或讲完后的检查题），答案由程序保管和判分，每轮最多一道。",
                     QUESTION_PARAMETERS, ask_question),
        ]

    async def _teach(self, messages, session_id, state, event_id, memory_key, returning_visitor, on_delta=None):
        # An authorization pause must not consume an answer or advance a quiz:
        # the original learner turn will run again once consent is resolved.
        permission_baseline = deepcopy(state)
        text = messages[-1]["text"].strip()
        budgets = self.budgets
        numbered = number_turns(messages)
        current_turn = numbered[-1]["turn"]
        context = state.setdefault("context", {})
        memory_enabled = not state.get("memory_blocked", False) and self.store.is_enabled(memory_key)
        memory = self.store.snapshot(memory_key) if memory_enabled else {
            "schema_version": "1.0", "profile": {}, "concepts": [], "error_records": [], "review_plan": [],
        }
        first_meeting = (not returning_visitor and "onboarding" not in state
                         and not any(item["role"] == "assistant" for item in messages)
                         and not (memory.get("profile") or memory.get("concepts")))
        previous_task, previous_quiz = state.get("active_task"), deepcopy(state.get("quiz"))
        quiz_feedback, quiz_evidence = self._grade_waiting(text, state)

        visible_state = deepcopy({key: value for key, value in state.items()
                                  if key not in ("last_event", "last_input", "last_response", "last_execution",
                                                 "context", "pending_external")})
        quiz = visible_state.get("quiz")
        if quiz and quiz.get("awaiting"):
            quiz["awaiting"] = public_quiz(quiz["awaiting"])
        focus = " ".join(filter(None, (state.get("active_task"), (state.get("quiz") or {}).get("topic"), text)))
        payload = {
            "messages": recent_window(numbered, budgets.decide_window, min_turns=budgets.min_turns,
                                      message_max=budgets.message_max),
            "learner": learner_card(state, memory, memory_enabled=memory_enabled),
            "state": visible_state, "memory": memory_slice(memory, focus, budgets.memory),
            "memory_enabled": memory_enabled, "first_meeting": first_meeting,
            "quiz_feedback": quiz_feedback,
            "external_declined": EXTERNAL_DECLINED.get(),
            "capabilities": {
                "research": {"available": self.provider is not None, "authorized": self.provider is not None and external_allowed("research")},
                "image_search": {"available": self.image_finder is not None, "authorized": self.image_finder is not None and external_allowed("image_search")},
                "image_generation": {"available": self.image_generator is not None, "authorized": self.image_generator is not None and external_allowed("image_generation")},
                "mcp": {"available": False, "authorized": False},
                "local_visual": {"available": self.artifacts is not None, "authorized": True},
            },
        }
        turn = _Turn()
        tools = self._tools(turn, session_id, state, text)
        if on_delta is not None and hasattr(self.agent, "stream_call"):
            drafted = await self.agent.stream_call("teach", payload, self.turn_schema, tools, on_delta=on_delta)
        else:
            drafted = await self.agent("teach", payload, self.turn_schema, tools)
        result = validate(drafted, self.turn_schema)
        if turn.denied_capabilities and not EXTERNAL_DECLINED.get():
            return self._request_external_permission(
                messages, permission_baseline, sorted(turn.denied_capabilities),
                "为当前问题补充外部资料或图片", result["response"]["intent"]["primary"])
        output, task = result["response"], result["task"]
        primary = output["intent"]["primary"]
        stopping = task["end_task"] or task["pause_task"] or result["stop_quiz"]
        self._apply_task(state, primary, {**task, "quiz_action": "stop" if result["stop_quiz"] else "none"})
        self._track_tasks(state, previous_task, previous_quiz, current_turn, text)
        research = turn.research()
        self._validate_response(output, output["intent"], research, stopping=stopping)
        if stopping:
            output["next_step"] = None
        elif turn.question is not None:
            output["next_step"] = {"kind": "question", "text": render_question(public_quiz(turn.question))}

        offer_notes = False
        if quiz_feedback and quiz_feedback["finished"]:
            results = context.get("quiz_results", [])
            done = state.get("quiz") or previous_quiz or {}
            results.append({"topic": done.get("topic"), "kind": quiz_feedback["kind"],
                            "answered": quiz_feedback["answered"], "correct": quiz_feedback["correct_count"],
                            "suspected_errors": done.get("errors", [])})
            context["quiz_results"] = results[-5:]
            if quiz_feedback["kind"] != "check" and turn.question is None:
                output["next_step"] = None
                if memory_enabled:
                    output["next_step"] = {"kind": "question", "text": NOTES_OFFER}
                    offer_notes = True
        waiting = (state.get("quiz") or {}).get("awaiting")
        if (waiting and not stopping and turn.question is None
                and (not output["next_step"] or output["next_step"]["kind"] != "question")):
            # A question the learner still owes an answer to stays in view.
            output["next_step"] = {"kind": "question", "text": render_question(public_quiz(waiting))}
        self._validate_response(output, output["intent"], research, stopping=stopping)

        if research is not None:
            labels = {"unavailable": "本次未取得可核查资料，未完成检索验证。",
                      "likely": "目前证据有限，尚未完成独立来源交叉核实。",
                      "disputed": "来源存在冲突，相关说法不能当作确定结论。"}
            if research["overall"] in labels:
                output["message"] += "\n\n" + labels[research["overall"]]
            sources = research["sources"][:3]
            if sources:
                output["message"] += "\n\n参考资料：" + "；".join(
                    f"[{source['title'].replace('[', '（').replace(']', '）')}]({source['url']})" for source in sources)
        if turn.artifact:
            output["message"] += f"\n\n[打开互动图]({turn.artifact['url']})"
        elif turn.visual_drafts:
            output["message"] += "\n\n这次未生成可用的互动图。"
        if turn.picture:
            output["message"] += f"\n\n[{QUIZ_PICTURE_LINK}]({turn.picture['url']})"
        validate(output, self.response_schema)

        # Nothing persistent is committed before the public output is valid.
        if turn.artifact:
            self.artifacts.commit(session_id, turn.artifact)
        if turn.picture:
            self.artifacts.commit(session_id, turn.picture)
        if quiz_evidence is not None and memory_enabled:
            try:
                turn_key = event_id if memory_key == session_id else f"{session_id}:{event_id}"
                self.store.apply_notes(memory_key, turn_key, empty_learning_notes(), text,
                                       quiz_evidence=quiz_evidence)
            except (ValueError, OSError, StorageError):
                output["message"] += "\n\n本轮做题记录未保存。"
        if result["observe_learning"] and memory_enabled:
            marks = [item for item in context.get("evidence_turns", []) if item != current_turn]
            context["evidence_turns"] = (marks + [current_turn])[-20:]
        if offer_notes:
            state["notes_offer"] = True
        state["last_execution"] = {"mode": "single", "tool_calls": turn.calls,
                                   "research": research["overall"] if research else "not_requested",
                                   "visual": "published" if turn.artifact else
                                   "unavailable" if turn.visual_drafts else "not_requested",
                                   "question": turn.question is not None}
        return output, state
